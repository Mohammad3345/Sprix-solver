import asyncio
import hashlib
import io
import json
import os
import re
import time
import gradio as gr
from PIL import Image
from playwright.async_api import async_playwright
from google import genai
from google.genai import types

PORTAL_URL = "https://sprixportal.jp"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

stop_flag = False
skip_flag = False
LIVE_HINT = ""
ACTIVE_MODEL = None
BAD_MODELS = set()
CACHED_MODELS = None
MEM_CACHE = {}

SUBMIT_BTNS = ["تحقق", "إرسال", "تأكيد", "تشغيل", "حفظ", "Check", "Submit", "Verify", "Run", "Confirm", "回答", "確認"]
NEXT_BTNS = ["التالي", "السؤال التالي", "الدرس التالي", "استمرار", "متابعة", "موافق", "حسناً", "Next", "Continue", "Proceed", "OK", "次へ"]
FAST_NEXT_EXACT = {"التالي", "السؤال التالي", "الدرس التالي", "استمرار", "متابعة", "Next", "Continue", "Proceed", "次へ"}

LOGIN_VIS_PROMPT = (
    'Look carefully at this screenshot. '
    'If the screen is mostly blank, white, empty, or loading, reply {"state":"LOADING"}. '
    'ONLY reply {"state":"LOGGED_IN"} if you clearly see actual student dashboard/lessons/courses content (NEVER if blank). '
    'If you see a button to open login/student portal/continue, reply {"state":"CLICK","x":500,"y":500}. '
    'If you see username/student-code/password fields, reply {"state":"LOGIN","ux":500,"uy":400,"px":500,"py":500,"bx":500,"by":600} '
    '(set px=-1 if only one input). Reply ONLY JSON.'
)

INJECT_CODE_JS = r"""(code) => {
 const aceEl = document.querySelector(".ace_editor");
 if (aceEl && window.ace) { try { const ed = window.ace.edit(aceEl); ed.setValue(code, -1); ed.clearSelection(); return "ACE"; } catch(e){} }
 if (window.monaco && window.monaco.editor) { try { const ms = window.monaco.editor.getModels(); if (ms && ms.length) { ms[0].setValue(code); return "MONACO"; } } catch(e){} }
 const cm = document.querySelector(".CodeMirror");
 if (cm && cm.CodeMirror) { try { cm.CodeMirror.setValue(code); return "CM"; } catch(e){} }
 const ta = Array.from(document.querySelectorAll("textarea")).find(el => el.offsetParent !== null && !el.classList.contains("ace_text-input"));
 if (ta) { ta.focus(); ta.value = code; ta.dispatchEvent(new Event("input", {bubbles:true})); ta.dispatchEvent(new Event("change", {bubbles:true})); return "TA"; }
 return null;
}"""

SNAP_JS = r"""() => {
 const txt = (document.body ? document.body.innerText : "").replace(/\n{3,}/g, "\n\n").trim().slice(0, 3200);
 const hasEd = Boolean(document.querySelector(".ace_editor, .monaco-editor, .CodeMirror, textarea:not([type=hidden])"));
 const hasChoices = Boolean(document.querySelector("input[type=radio], input[type=checkbox]"));
 const btns = Array.from(document.querySelectorAll("button, a, [role=button]"))
   .filter(el => el.offsetParent !== null)
   .map(el => (el.innerText || "").trim())
   .filter(t => t && t.length <= 45)
   .slice(0, 25);
 let existingCode = "";
 const aceEl = document.querySelector(".ace_editor");
 if (aceEl && window.ace) { try { existingCode = window.ace.edit(aceEl).getValue() || ""; } catch(e){} }
 else if (window.monaco && window.monaco.editor) { try { const ms = window.monaco.editor.getModels(); if (ms && ms.length) existingCode = ms[0].getValue() || ""; } catch(e){} }
 else {
   const cm = document.querySelector(".CodeMirror");
   if (cm && cm.CodeMirror) { try { existingCode = cm.CodeMirror.getValue() || ""; } catch(e){} }
 }
 return {txt, hasEd, hasChoices, btns, existingCode: existingCode.slice(0, 1200)};
}"""

FIND_BTN_JS = r"""([isProg]) => {
 const norm = s => (s||"").replace(/[\u064B-\u065F\u0670\u0640]/g,"").replace(/[أإآ]/g,"ا").replace(/ى/g,"ي").replace(/ة/g,"ه").replace(/\s+/g," ").trim().toLowerCase();
 const secKw = isProg ? "البرمجه" : "التثقيف المالي";
 const platKw = isProg ? "منصه البرمجه" : "منصه التعلم";
 const allH = ["البرمجه وتكنولوجيا المعلومات", "التثقيف المالي", "الثقافه الماليه"];
 document.querySelectorAll("[data-bot-btn]").forEach(e=>e.removeAttribute("data-bot-btn"));
 const hs = [];
 for (const el of document.querySelectorAll("h1,h2,h3,h4,h5,div,p,span,section")) {
  const t = norm(el.innerText);
  if (!t || t.length > 70 || el.children.length > 2) continue;
  const r = el.getBoundingClientRect();
  if (r.width && r.height) hs.push({el, t, top: r.top + window.scrollY});
 }
 const th = hs.find(h => h.t.includes(secKw));
 const tTop = th ? th.top : null;
 const below = tTop !== null ? hs.filter(h => h.el !== th.el && allH.some(k => h.t.includes(k)) && h.top > tTop + 50).map(h => h.top).sort((a,b)=>a-b) : [];
 const nTop = below.length ? below[0] : null;
 const cands = [];
 for (const btn of document.querySelectorAll("a,button,[role=button]")) {
  const st = window.getComputedStyle(btn);
  if (st.display==="none"||st.visibility==="hidden"||st.pointerEvents==="none"||btn.disabled||parseFloat(st.opacity||"1")<0.4) continue;
  const r = btn.getBoundingClientRect();
  if (r.width<25||r.height<18||r.width>420||r.height>120) continue;
  const bTop = r.top + window.scrollY;
  if (tTop !== null && (bTop < tTop - 10 || (nTop !== null && bTop > nTop - 5) || (nTop === null && bTop > tTop + 520))) continue;
  let card = btn;
  for (let i=0;i<5&&card.parentElement;i++){const pr=card.parentElement.getBoundingClientRect(); if(pr.width>460||pr.height>460)break; card=card.parentElement;}
  const im = Array.from(card.querySelectorAll("img")).map(x=>(x.getAttribute("alt")||"")+" "+(x.getAttribute("src")||"")).join(" ");
  const a = btn.tagName.toLowerCase()==="a" ? btn : (btn.closest("a") || card.querySelector("a[href]"));
  const href = a ? (a.href || a.getAttribute("href") || "") : "";
  cands.push({btn, href, meta: norm([card.innerText||"", btn.innerText||"", href, im].join(" ")), txt: norm(btn.innerText)});
 }
 if (!cands.length) return null;
 const ch = cands.find(c => c.meta.includes(platKw)) || cands.find(c => c.meta.includes("sprix")) || cands.find(c => c.txt.includes(norm("تسجيل الدخول"))) || cands[0];
 ch.btn.setAttribute("data-bot-btn", "1");
 ch.btn.scrollIntoView({block: "center"});
 const fr = ch.btn.getBoundingClientRect();
 return {x: fr.left + fr.width/2, y: fr.top + fr.height/2, href: ch.href};
}"""


def clean_secret(v, min_len=15):
    return next((x for x in (v or "").split() if len(x) > min_len), (v or "").strip())


def get_api_key(ui_key=""):
    for raw in (ui_key, os.environ.get("GEMINI_API_KEY", "")):
        k = clean_secret(raw, 15)
        if k:
            return k
    return ""


def stop_bot():
    global stop_flag
    stop_flag = True
    return "تم طلب الإيقاف الفوري..."


def skip_step():
    global skip_flag
    skip_flag = True
    return "سيتم تخطي الخطوة الحالية..."


def set_live_hint(h):
    global LIVE_HINT
    LIVE_HINT = (h or "").strip()
    return f"تم حفظ التوجيه: {LIVE_HINT}" if LIVE_HINT else "تم مسح التوجيه"


def parse_json(raw):
    if not raw:
        return None
    t = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    m = re.search(r"\{[\s\S]*\}", t)
    for cand in (t, m.group(0) if m else None):
        if not cand:
            continue
        try:
            obj = json.loads(cand)
            if isinstance(obj, list):
                obj = next((x for x in obj if isinstance(x, dict)), None)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


def lang_rules(subject, prog_lang):
    if "التثقيف المالي" in subject:
        return "Subject: Financial Literacy (Sprix). Choose the exact correct option text."
    if "Python" in prog_lang:
        return (
            "Subject: Python 3 Programming (Sprix/QUREO). "
            "Write clean, runnable Python 3 code with exact 4-space indentation, no markdown backticks, "
            "and preserve any required variable names or starter structure."
        )
    if "Java" in prog_lang:
        return (
            "Subject: Java Programming (Sprix/QUREO). "
            "Write valid, compilable Java code with exact semicolons, braces, and class/main structure "
            "(matching any starter code in the editor). No markdown backticks."
        )
    return (
        "Subject: Programming (Python / Java / JavaScript on Sprix/QUREO). "
        "Detect the language from the question and starter code. "
        "For Python use exact 4-space indentation; for Java use valid class/method syntax and semicolons. "
        "Return ONLY raw runnable code without markdown backticks."
    )


def get_models(client):
    global CACHED_MODELS
    if CACHED_MODELS is None:
        pref = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3-flash-preview", "gemini-2.0-flash"]
        found = []
        try:
            for m in client.models.list():
                nm = (getattr(m, "name", "") or "").replace("models/", "").strip()
                acts = getattr(m, "supported_actions", None) or []
                low = nm.lower()
                if "gemini" not in low or (acts and "generateContent" not in acts):
                    continue
                if any(b in low for b in ("embedding", "tts", "imagen", "veo", "audio", "live", "aqa", "robotics", "computer-use", "argon", "ultra", "pro")):
                    continue
                found.append(nm)
            found.sort(
                key=lambda x: (
                    1 if "2.5-flash" in x.lower() else 0,
                    1 if "flash" in x.lower() else 0,
                    float((re.findall(r"\d+(?:\.\d+)?", x) or [0])[0]),
                    1 if ("preview" not in x.lower() and "exp" not in x.lower()) else 0,
                ),
                reverse=True,
            )
        except Exception:
            pass
        merged = []
        for nm in found + pref:
            if nm and nm not in merged:
                merged.append(nm)
        CACHED_MODELS = merged
    return [m for m in CACHED_MODELS if m not in BAD_MODELS]


def _build_cfg(use_thinking_zero=True):
    kw = {"temperature": 0.1, "max_output_tokens": 900, "response_mime_type": "application/json"}
    if use_thinking_zero and hasattr(types, "ThinkingConfig"):
        kw["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    return types.GenerateContentConfig(**kw)


async def ask_ai_fast(client, prompt, img_bytes=None, cache_key=None):
    global ACTIVE_MODEL
    if cache_key and cache_key in MEM_CACHE:
        return MEM_CACHE[cache_key]
    tried = set()
    q = [ACTIVE_MODEL] if (ACTIVE_MODEL and ACTIVE_MODEL not in BAD_MODELS) else []
    for m in get_models(client):
        if m not in q:
            q.append(m)
    contents = [types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"), prompt] if img_bytes else [prompt]
    last_err = None
    idx = 0
    while idx < len(q):
        mn = q[idx]
        idx += 1
        if mn in tried:
            continue
        tried.add(mn)
        for fast_cfg in (True, False):
            try:
                cfg = _build_cfg(fast_cfg)
                if hasattr(client, "aio"):
                    r = await client.aio.models.generate_content(model=mn, contents=contents, config=cfg)
                else:
                    r = await asyncio.to_thread(client.models.generate_content, model=mn, contents=contents, config=cfg)
                ACTIVE_MODEL = mn
                parsed = parse_json((r.text or "").strip())
                if parsed and cache_key and parsed.get("action") in ("CLICK", "CODE"):
                    MEM_CACHE[cache_key] = parsed
                return parsed
            except Exception as e:
                last_err = e
                es = str(e).lower()
                if fast_cfg and ("thinking" in es or "unknown field" in es or "invalid" in es):
                    continue
                rec = re.search(r"use\s+(?:models/)?([a-zA-Z0-9._-]+)", str(e))
                if rec:
                    sg = rec.group(1).replace("models/", "").strip()
                    if sg and sg not in tried and sg not in BAD_MODELS:
                        q.insert(idx, sg)
                if any(k in es for k in ("404", "not_found", "no longer available", "429", "resource_exhausted", "quota", "limit: 0", "403", "permission_denied", "503", "unavailable")):
                    if any(k in es for k in ("404", "not_found", "limit: 0", "no longer available", "403", "permission_denied")):
                        BAD_MODELS.add(mn)
                        if ACTIVE_MODEL == mn:
                            ACTIVE_MODEL = None
                    break
                raise
    raise RuntimeError("فشل الاتصال بنموذج Gemini: " + str(last_err))


async def click_text(page, texts, timeout=1800):
    for ex in (True, False):
        for t in texts:
            if not t:
                continue
            try:
                loc = page.get_by_text(t, exact=ex).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=timeout)
                    return True
            except Exception:
                pass
    return False


async def click_xy(page, x, y):
    vp = page.viewport_size or {"width": 1280, "height": 800}
    px = max(5, min(vp["width"] - 5, int(x / 1000.0 * vp["width"])))
    py = max(5, min(vp["height"] - 5, int(y / 1000.0 * vp["height"])))
    await page.mouse.click(px, py)


async def wait_ready(page, max_s=14):
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    t0 = time.time()
    reloaded = False
    while time.time() - t0 < max_s:
        try:
            n = await page.evaluate(
                "() => (document.body ? document.body.innerText.trim().length : 0) + document.querySelectorAll('input,button,a,iframe').length * 10"
            )
            if n > 25:
                await asyncio.sleep(0.35)
                return True
        except Exception:
            pass
        if not reloaded and time.time() - t0 > 7:
            reloaded = True
            try:
                await page.reload(wait_until="domcontentloaded", timeout=12000)
            except Exception:
                pass
        await asyncio.sleep(0.35)
    return False


async def try_dom_login(page, code, pwd):
    u_sels = [
        "input[name*=login i]", "input[name*=user i]", "input[name*=student i]",
        "input[name*=code i]", "input[name=id]", "input[type=email]",
        "input[type=text]", "input[type=tel]", "input[type=number]",
    ]
    p_sels = ["input[type=password]", "input[name*=pass i]"]
    b_sels = [
        "button[type=submit]", "input[type=submit]",
        "button:has-text('تسجيل الدخول')", "button:has-text('دخول')",
        "button:has-text('Login')", "button:has-text('Sign in')", "button:has-text('ログイン')",
    ]
    for fr in [page] + [f for f in page.frames if f != page.main_frame]:
        u_el = p_el = b_el = None
        for s in u_sels:
            loc = fr.locator(s).first
            if await loc.count() > 0 and await loc.is_visible():
                u_el = loc
                break
        for s in p_sels:
            loc = fr.locator(s).first
            if await loc.count() > 0 and await loc.is_visible():
                p_el = loc
                break
        for s in b_sels:
            loc = fr.locator(s).first
            if await loc.count() > 0 and await loc.is_visible():
                b_el = loc
                break
        if u_el and p_el:
            await u_el.fill(code.strip())
            await p_el.fill(pwd)
            if b_el:
                await b_el.click()
            else:
                await p_el.press("Enter")
            return "BOTH"
        if u_el and not p_el:
            await u_el.fill(code.strip())
            if b_el:
                await b_el.click()
            else:
                await u_el.press("Enter")
            return "USER_ONLY"
    return None


def fast_continue_btn(snap):
    if snap.get("hasEd") or snap.get("hasChoices"):
        return None
    if len(snap.get("txt", "")) > 260:
        return None
    for b in snap.get("btns", []):
        if b.strip() in FAST_NEXT_EXACT:
            return b.strip()
    return None


async def run_auto_solver(subject, prog_lang, student_code, password, grade, edu_type, speed_mode, custom_url, ui_api_key):
    global stop_flag, skip_flag, LIVE_HINT
    stop_flag, skip_flag, logs, img = False, False, [], None
    t_start = time.time()
    fast_delay = 0.25 if "فائق" in speed_mode else 0.75

    def log(m):
        logs.append(f"[{int(time.time()-t_start):02d}s] {m}")
        return "\n".join(logs[-18:])

    def stats(solved, state_lbl):
        return f"الحالة: {state_lbl} | المنجز: {solved} خطوة | الزمن: {int(time.time()-t_start)} ثانية | النموذج: {ACTIVE_MODEL or 'تلقائي'}"

    if not student_code.strip() or not password:
        yield log("اكتب كود الطالب وكلمة المرور أولاً."), None, stats(0, "بانتظار البيانات")
        return

    api_key = get_api_key(ui_api_key)
    if not api_key:
        yield log("مفتاح GEMINI_API_KEY غير موجود! أضفه في Environment Variables أو في الإعدادات المتقدمة."), None, stats(0, "خطأ مفتاح API")
        return

    client = genai.Client(api_key=api_key)
    is_prog = "البرمجة" in subject
    rules_txt = lang_rules(subject, prog_lang)

    try:
        yield log(f"تشغيل المتصفح ({subject} - {prog_lang})..."), None, stats(0, "جاري التشغيل")
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox", "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--enable-webgl", "--use-gl=angle", "--use-angle=swiftshader",
                ],
            )
            ctx = await browser.new_context(viewport={"width": 1280, "height": 800}, user_agent=UA, locale="ar-EG", ignore_https_errors=True)
            await ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            await ctx.route("**/*", lambda route: route.abort() if route.request.resource_type in ("media",) else route.continue_())
            page = await ctx.new_page()

            start_url = (custom_url or "").strip() or PORTAL_URL
            await page.goto(start_url, timeout=45000, wait_until="domcontentloaded")
            await wait_ready(page, 10)

            if start_url == PORTAL_URL:
                await click_text(page, [grade, "الصف الثاني الثانوي"])
                await asyncio.sleep(0.3)
                await click_text(page, [edu_type, "التعليم العام"])
                await asyncio.sleep(0.5)
                old_url = page.url
                dom = await page.evaluate(FIND_BTN_JS, [is_prog])
                if dom:
                    href = (dom.get("href") or "").strip()
                    if href.startswith("http") and href.rstrip("/") != old_url.rstrip("/"):
                        await page.goto(href, timeout=45000, wait_until="domcontentloaded")
                    else:
                        try:
                            async with ctx.expect_page(timeout=3500) as npi:
                                await page.locator("[data-bot-btn=1]").first.click(timeout=3500)
                            page = await npi.value
                        except Exception:
                            try:
                                await page.mouse.click(dom["x"], dom["y"])
                            except Exception:
                                pass
                await wait_ready(page, 16)

            jpg = await page.screenshot(type="jpeg", quality=55)
            img = Image.open(io.BytesIO(jpg))
            yield log("فتح المنصة — تسجيل الدخول السريع..."), img, stats(0, "تسجيل الدخول")

            for attempt in range(1, 5):
                if stop_flag:
                    yield log("تم الإيقاف."), img, stats(0, "متوقف")
                    await browser.close()
                    return
                res = await try_dom_login(page, student_code, password)
                if res == "BOTH":
                    await asyncio.sleep(2.2)
                    await wait_ready(page, 10)
                    if await page.locator("input[type=password]").count() == 0:
                        jpg = await page.screenshot(type="jpeg", quality=55)
                        img = Image.open(io.BytesIO(jpg))
                        yield log("تم الدخول بنجاح! بدء الحل السريع..."), img, stats(0, "جاري الحل")
                        break
                elif res == "USER_ONLY":
                    await asyncio.sleep(2)
                    await wait_ready(page, 8)
                    continue

                jpg = await page.screenshot(type="jpeg", quality=55)
                img = Image.open(io.BytesIO(jpg))
                raw_p = await ask_ai_fast(client, LOGIN_VIS_PROMPT, jpg)
                plan = raw_p if isinstance(raw_p, dict) else {}
                st = str(plan.get("state", "")).upper()
                if st == "LOGGED_IN":
                    yield log("تم تأكيد الدخول للمنصة."), img, stats(0, "جاري الحل")
                    break
                elif st == "LOADING":
                    await asyncio.sleep(2)
                    if attempt == 2:
                        await page.reload(wait_until="domcontentloaded")
                        await wait_ready(page, 10)
                elif st == "CLICK" and int(plan.get("x", -1)) >= 0:
                    await click_xy(page, int(plan["x"]), int(plan["y"]))
                    await asyncio.sleep(2)
                    await wait_ready(page, 8)
                elif st == "LOGIN" and int(plan.get("ux", -1)) >= 0:
                    await click_xy(page, int(plan["ux"]), int(plan["uy"]))
                    await page.keyboard.press("Control+A")
                    await page.keyboard.type(student_code.strip(), delay=5)
                    if int(plan.get("px", -1)) >= 0:
                        await click_xy(page, int(plan["px"]), int(plan["py"]))
                        await page.keyboard.press("Control+A")
                        await page.keyboard.type(password, delay=5)
                    if int(plan.get("bx", -1)) >= 0:
                        await click_xy(page, int(plan["bx"]), int(plan["by"]))
                    else:
                        await page.keyboard.press("Enter")
                    await asyncio.sleep(2.5)
                    await wait_ready(page, 10)
                else:
                    await asyncio.sleep(1.5)

            last_sig, stuck, solved = "", 0, 0
            for step in range(1, 151):
                if stop_flag:
                    yield log("تم الإيقاف بواسطة المستخدم."), img, stats(solved, "متوقف")
                    break
                if skip_flag:
                    skip_flag = False
                    await click_text(page, NEXT_BTNS, 1200)
                    yield log("تم تخطي الخطوة الحالية."), img, stats(solved, "تخطي")
                    continue

                await asyncio.sleep(fast_delay)
                snap = await page.evaluate(SNAP_JS)
                sig = hashlib.md5((page.url + "|" + snap["txt"][:400]).encode("utf-8", "ignore")).hexdigest()
                if sig == last_sig:
                    stuck += 1
                else:
                    stuck = 0
                    last_sig = sig

                quick_btn = fast_continue_btn(snap) if stuck == 0 else None
                hint_txt = f" User guidance: {LIVE_HINT}." if LIVE_HINT else ""
                starter = f"\nStarterCodeInEditor:\n{snap['existingCode']}" if snap.get("existingCode") else ""

                if quick_btn:
                    act = {"action": "CLICK", "text": quick_btn, "why": "انتقال سريع"}
                elif (len(snap["txt"]) < 35) or (stuck >= 2):
                    jpg = await page.screenshot(type="jpeg", quality=50)
                    img = Image.open(io.BytesIO(jpg))
                    v_prompt = (
                        f"Look carefully at this lesson screenshot. {rules_txt}{hint_txt}{starter} "
                        'If blank/white/loading, reply {"action":"WAIT","why":"loading"}. '
                        'ONLY reply {"action":"DONE","why":"unit finished"} if completion/score is explicitly shown (NEVER if blank). '
                        'If a code editor needs code, reply {"action":"CODE","code":"exact code","x":500,"y":400,"sx":-1,"sy":-1,"why":"code"}. '
                        'Otherwise reply {"action":"CLICK","text":"exact button/choice text","x":500,"y":500,"why":"click"} or {"action":"SCROLL","dy":450,"why":"scroll"}. Reply ONLY JSON.'
                    )
                    raw_a = await ask_ai_fast(client, v_prompt, jpg)
                    act = raw_a if isinstance(raw_a, dict) else {"action": "WAIT", "why": "vision"}
                else:
                    t_prompt = (
                        f"Ultra-fast auto-solver. {rules_txt}{hint_txt} "
                        f"Buttons: {snap['btns']}. HasCodeEditor: {snap['hasEd']}.{starter}\n"
                        f"PageText:\n{snap['txt']}\n\n"
                        'Reply ONLY valid JSON: '
                        '{"action":"WAIT","why":"empty"} if PageText is empty/loading; '
                        '{"action":"DONE","why":"all finished"} ONLY if lesson/unit completion is explicitly shown; '
                        '{"action":"CODE","code":"exact final code","why":"code"} if code editor needs code; '
                        '{"action":"CLICK","text":"exact button or choice text from page","why":"answer or next"}.'
                    )
                    ck = None if LIVE_HINT else hashlib.md5(t_prompt.encode("utf-8", "ignore")).hexdigest()
                    raw_a = await ask_ai_fast(client, t_prompt, None, cache_key=ck)
                    act = raw_a if isinstance(raw_a, dict) else {"action": "WAIT", "why": "text"}

                kind = str(act.get("action", "WAIT")).upper()
                why = str(act.get("why", ""))
                try:
                    if kind == "DONE" and (solved > 0 or len(snap.get("txt", "")) > 40):
                        jpg = await page.screenshot(type="jpeg", quality=55)
                        img = Image.open(io.BytesIO(jpg))
                        yield log(f"اكتمل الحل بنجاح في {int(time.time()-t_start)} ثانية! (تم إنجاز {solved} خطوة)"), img, stats(solved, "مكتمل")
                        break
                    elif kind == "WAIT":
                        await asyncio.sleep(1.5)
                    elif kind == "SCROLL":
                        await page.mouse.wheel(0, int(act.get("dy", 450)))
                    elif kind == "CODE":
                        code_txt = str(act.get("code", "")).strip()
                        mode = await page.evaluate(INJECT_CODE_JS, code_txt)
                        if not mode:
                            if int(act.get("x", -1)) >= 0:
                                await click_xy(page, int(act["x"]), int(act["y"]))
                            await page.keyboard.press("Control+A")
                            await page.keyboard.press("Backspace")
                            await page.keyboard.type(code_txt, delay=1)
                        await asyncio.sleep(0.25)
                        if int(act.get("sx", -1)) >= 0:
                            await click_xy(page, int(act["sx"]), int(act["sy"]))
                        else:
                            await click_text(page, SUBMIT_BTNS, 1000)
                        await asyncio.sleep(0.7)
                        await click_text(page, NEXT_BTNS, 1000)
                        solved += 1
                        if solved % 3 == 1:
                            jpg = await page.screenshot(type="jpeg", quality=50)
                            img = Image.open(io.BytesIO(jpg))
                        yield log(f"سؤال #{solved}: تم إدخال كود ({prog_lang})"), img, stats(solved, "يعمل")
                    elif kind == "CLICK":
                        txt = str(act.get("text", "")).strip()
                        ok = await click_text(page, [txt], 1500) if txt else False
                        if not ok and int(act.get("x", -1)) >= 0:
                            await click_xy(page, int(act["x"]), int(act["y"]))
                            ok = True
                        if ok:
                            await asyncio.sleep(0.3)
                            await click_text(page, SUBMIT_BTNS, 900)
                            await asyncio.sleep(0.5)
                            await click_text(page, NEXT_BTNS, 900)
                            solved += 1
                            if solved % 3 == 1:
                                jpg = await page.screenshot(type="jpeg", quality=50)
                                img = Image.open(io.BytesIO(jpg))
                            yield log(f"خطوة #{solved}: {txt or why}"), img, stats(solved, "يعمل")
                        else:
                            stuck += 1
                    if len(ctx.pages) > 1:
                        page = ctx.pages[-1]
                except Exception as e:
                    stuck += 1
                    yield log(f"تجاوز خطأ عابر: {str(e)[:45]}"), img, stats(solved, "تعافي تلقائي")

                if stuck >= 7:
                    jpg = await page.screenshot(type="jpeg", quality=55)
                    img = Image.open(io.BytesIO(jpg))
                    yield log("توقف البوت لعدم تغيّر الصفحة، راجع الشاشة بالأسفل أو أضف توجيهاً."), img, stats(solved, "متوقف")
                    break
            await browser.close()
    except Exception as e:
        yield log(f"خطأ: {e}"), img, stats(0, "خطأ")
    yield log("انتهت الجلسة."), img, stats(0, "انتهت الجلسة")


async def solve_code_directly(question_text, code_lang, ui_api_key):
    if not (question_text or "").strip():
        return "اكتب نص السؤال البرمجي أولاً."
    api_key = get_api_key(ui_api_key)
    if not api_key:
        return "مفتاح GEMINI_API_KEY غير موجود في Environment Variables أو في الإعدادات."
    client = genai.Client(api_key=api_key)
    lang_note = (
        "Python 3 (with exact 4-space indentation)" if "Python" in code_lang
        else "Java (complete compilable class/method with semicolons)"
    )
    prompt = (
        f"Solve this programming question in {lang_note}.\n"
        f"Question:\n{question_text}\n\n"
        'Reply ONLY valid JSON: {"code": "exact clean runnable code only", "explanation": "brief Arabic explanation in 2 lines"}'
    )
    try:
        res = await ask_ai_fast(client, prompt, None)
        if not isinstance(res, dict):
            return "تعذر تحليل الإجابة، حاول مرة أخرى."
        code = (res.get("code") or "").strip()
        exp = (res.get("explanation") or "").strip()
        fence = "python" if "Python" in code_lang else "java"
        return f"```{fence}\n{code}\n```\n\n**الشرح المختصر:** {exp}"
    except Exception as e:
        return f"حدث خطأ: {e}"


with gr.Blocks(title="SPRIX Auto-Solver (Python & Java)") as demo:
    gr.HTML(
        '<style>.gradio-container{direction:rtl;text-align:right;max-width:780px!important;margin:auto;}</style>'
        '<div style="text-align:center;padding:8px 0;">'
        '<h2 style="color:#10b981;margin:0;">منصة SPRIX الذكية — حل تلقائي (Python & Java)</h2>'
        '<p style="color:#94a3b8;margin:4px 0 0;">تسجيل دخول وحل تلقائي للمنصة + محرّر حل مباشر لبرمجة بايثون وجافا</p>'
        '</div>'
    )

    with gr.Tabs():
        with gr.Tab("الحل التلقائي للمنصة (Sprix / QUREO)"):
            with gr.Row():
                subject = gr.Radio(["البرمجة وتكنولوجيا المعلومات", "التثقيف المالي"], value="البرمجة وتكنولوجيا المعلومات", label="المادة الدراسية")
                prog_lang = gr.Radio(["تلقائي (Python / Java)", "Python (بايثون)", "Java (جافا)"], value="تلقائي (Python / Java)", label="لغة البرمجة")
            with gr.Row():
                student_code = gr.Textbox(label="كود الطالب", placeholder="أدخل كود الطالب")
                password = gr.Textbox(label="كلمة المرور", type="password", placeholder="أدخل كلمة المرور")
            with gr.Row():
                start_btn = gr.Button("ابدأ التسجيل والحل التلقائي", variant="primary")
                skip_btn = gr.Button("تخطي الخطوة", variant="secondary")
                stop_btn = gr.Button("إيقاف فوري", variant="stop")
            status_bar = gr.Textbox(label="مؤشر الأداء المباشر", value="الحالة: جاهز | المنجز: 0 خطوة", interactive=False)
            with gr.Accordion("تحكم مرن وإعدادات متقدمة (اختياري)", open=False):
                with gr.Row():
                    live_hint_in = gr.Textbox(label="توجيه مرن للبوت", placeholder="مثال: ادخل على الوحدة الثانية / اكتب الحل بلغة جافا")
                    hint_btn = gr.Button("تطبيق التوجيه")
                hint_status = gr.Textbox(label="حالة التوجيه", interactive=False)
                with gr.Row():
                    grade = gr.Dropdown(["الصف الثاني الثانوي", "الصف الأول الثانوي"], value="الصف الثاني الثانوي", label="الصف الدراسي")
                    edu_type = gr.Dropdown(["التعليم العام", "التعليم الفني", "الأزهر"], value="التعليم العام", label="نوع التعليم")
                    speed_mode = gr.Radio(["فائق السرعة (موصى به)", "متوازن"], value="فائق السرعة (موصى به)", label="السرعة")
                with gr.Row():
                    custom_url = gr.Textbox(label="رابط مباشر مخصص (اختياري)", placeholder="https://sprixportal.jp")
                    ui_api_key = gr.Textbox(label="مفتاح Gemini API (اختياري إذا لم يُضف في Secrets)", type="password")
            logs_out = gr.Textbox(label="سجل العمليات المباشر", lines=9, interactive=False)
            screen_out = gr.Image(label="شاشة المنصة المباشرة")

            start_btn.click(
                fn=run_auto_solver,
                inputs=[subject, prog_lang, student_code, password, grade, edu_type, speed_mode, custom_url, ui_api_key],
                outputs=[logs_out, screen_out, status_bar],
            )
            skip_btn.click(fn=skip_step, outputs=hint_status)
            stop_btn.click(fn=stop_bot, outputs=hint_status)
            hint_btn.click(fn=set_live_hint, inputs=[live_hint_in], outputs=[hint_status])

        with gr.Tab("حل مباشر لبرمجة Python و Java"):
            code_lang = gr.Radio(["Python (بايثون)", "Java (جافا)"], value="Python (بايثون)", label="اختر لغة البرمجة")
            q_input = gr.Textbox(label="نص السؤال البرمجي أو الكود المطلوب تصحيحه", lines=5, placeholder="الصق السؤال هنا...")
            solve_btn = gr.Button("احصل على الكود الصحيح فوراً", variant="primary")
            code_output = gr.Markdown(label="الحل البرمجي")
            solve_btn.click(fn=solve_code_directly, inputs=[q_input, code_lang, ui_api_key], outputs=[code_output])

if __name__ == "__main__":
    demo.queue().launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 7860)))
