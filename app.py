# -*- coding: utf-8 -*-
"""
购买决策分析工具 —— 本地自包含版后端
- 零第三方依赖（纯 Python 标准库）
- 联网搜索：抓取 cn.bing.com 搜索结果并解析（免 API key）
- AI 研判：调用 OpenAI 兼容的云端大模型 API（DeepSeek / 豆包方舟 / 通义等，需在设置中配置）
- 数据：配置保存在本地 config.json；历史记录保存在浏览器 localStorage
"""
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "购买决策分析工具"
# 冻结（EXE）与源码运行环境兼容：
# - 配置/数据文件始终保存在 EXE 所在目录，保证持久化
# - 打包进 EXE 的 web 静态资源从 _MEIPASS 读取
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
    _RES_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _RES_DIR = BASE_DIR
WEB_DIR = os.path.join(_RES_DIR, "web")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "provider": "deepseek",
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
}

PROVIDERS = [
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    {"id": "doubao", "name": "豆包·火山方舟", "base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": "ep-模型接入点ID"},
    {"id": "qwen", "name": "通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    {"id": "openai", "name": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    {"id": "custom", "name": "自定义", "base_url": "", "model": ""},
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


# ============================================================
# 联网搜索
# ============================================================
class BingSearch:
    """抓取 cn.bing.com 搜索结果（免 key），解析标题/链接/摘要。"""

    def __init__(self, timeout=15):
        self.timeout = timeout

    def search(self, query, n=8):
        url = "https://cn.bing.com/search?" + urllib.parse.urlencode(
            {"q": query, "count": str(n)})
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml",
        })
        try:
            html = urllib.request.urlopen(req, timeout=self.timeout).read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            raise RuntimeError("搜索服务返回异常状态码：%s" % e.code)
        except urllib.error.URLError as e:
            raise RuntimeError("无法连接搜索服务：%s" % (e.reason or "网络错误"))
        except Exception as e:
            raise RuntimeError("搜索请求失败：%s" % e)

        results = []
        blocks = re.findall(r'<li class="b_algo".*?</li>', html, re.S)
        for b in blocks:
            m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
            if not m:
                continue
            url_raw = m.group(1).strip()
            title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            if not url_raw or not title:
                continue
            m2 = re.search(r'<p[^>]*>(.*?)</p>', b, re.S)
            snippet = re.sub(r'<[^>]+>', '', m2.group(1)).strip() if m2 else ""
            results.append({"title": title, "url": url_raw, "snippet": snippet})
            if len(results) >= n:
                break
        if not results:
            raise RuntimeError("未搜索到相关结果，请检查商品名称拼写或换个说法")
        return results


# 详情页识别：标题/URL 命中这些词则优先（参数、报价、评测等），命中降级词则靠后
_DETAIL_HIT = ["参数", "报价", "评测", "配置", "详情", "价格", "多少钱",
               "specs", "products", "product", "detail", "中关村", "zol",
               "京东", "jd.com", "苏宁", "太平洋", "pcpop", "什么值得买",
               "smzdm", "百科", "baike"]
_DETAIL_PAT = re.compile(r"/(?:products?|specs?|detail|item|view)/\d+", re.I)
_LOW_HIT = ["career", "招聘", "cloud", "云服务", "support", "客户服务",
            "关于", "about", "登录", "注册", "/en/", "global", "首页"]

EXTRA_QUERIES = [
    " 参数 配置 报价 详情",
    " 评测 口碑 值得买吗 价格",
    " site:zol.com.cn 参数",
    " site:jd.com 价格",
]


def _rank_result(title, url):
    """对搜索结果打分：详情页靠前、无关官网首页靠后。"""
    t = title.lower()
    u = url.lower()
    score = 0
    for w in _DETAIL_HIT:
        if w in t or w in u:
            score += 1
    if _DETAIL_PAT.search(u):
        score += 2
    for w in _LOW_HIT:
        if w in t or w in u:
            score -= 1
    return score


class BingSearch:
    """抓取 cn.bing.com 搜索结果（免 key），解析标题/链接/摘要。"""

    def __init__(self, timeout=15):
        self.timeout = timeout

    def search(self, query, n=8):
        url = "https://cn.bing.com/search?" + urllib.parse.urlencode(
            {"q": query, "count": str(n)})
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml",
        })
        try:
            html = urllib.request.urlopen(req, timeout=self.timeout).read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            raise RuntimeError("搜索服务返回异常状态码：%s" % e.code)
        except urllib.error.URLError as e:
            raise RuntimeError("无法连接搜索服务：%s" % (e.reason or "网络错误"))
        except Exception as e:
            raise RuntimeError("搜索请求失败：%s" % e)

        results = []
        blocks = re.findall(r'<li class="b_algo".*?</li>', html, re.S)
        for b in blocks:
            m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
            if not m:
                continue
            url_raw = m.group(1).strip()
            title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            if not url_raw or not title:
                continue
            m2 = re.search(r'<p[^>]*>(.*?)</p>', b, re.S)
            snippet = re.sub(r'<[^>]+>', '', m2.group(1)).strip() if m2 else ""
            results.append({"title": title, "url": url_raw, "snippet": snippet})
            if len(results) >= n:
                break
        if not results:
            raise RuntimeError("未搜索到相关结果，请检查商品名称拼写或换个说法")
        return results

    def search_multi(self, item, suffix="", n=8):
        """多组查询并行搜索 → 合并去重 → 详情页优先排序。"""
        queries = [item + suffix] + [item + q for q in EXTRA_QUERIES]
        merged = {}

        def run(q):
            try:
                return self.search(q, n=n)
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=4) as ex:
            for batch in ex.map(run, queries):
                for r in batch:
                    key = r["url"].split("?")[0].rstrip("/").split("#")[0]
                    if key not in merged:
                        merged[key] = r
        if not merged:
            raise RuntimeError("未搜索到相关结果，请检查商品名称拼写或换个说法")
        ranked = sorted(merged.values(), key=lambda r: _rank_result(r["title"], r["url"]), reverse=True)
        return ranked[:n]


# 页面正文中值得提取的关键词（价格/参数/评价相关）
_PAGE_KWS = ["价格", "¥", "￥", "元", "起售", "官方价", "优惠", "分期",
             "配置", "参数", "内存", "存储", "电池", "处理器", "屏幕",
             "摄像头", "像素", "英寸", "充电", "续航", "重量", "版本",
             "规格", "保修", "售后", "评价", "口碑", "缺点", "优点", "值得买"]


class PageExtractor:
    """抓取详情页正文，用关键词窗口提取价格/参数/评价等关键信息。"""

    def __init__(self, timeout=10, max_hits=12, max_chars=1200):
        self.timeout = timeout
        self.max_hits = max_hits
        self.max_chars = max_chars

    def extract(self, url):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        try:
            raw = urllib.request.urlopen(req, timeout=self.timeout).read()
            try:
                html = raw.decode("utf-8")
            except UnicodeDecodeError:
                html = raw.decode("gbk", "replace")
        except urllib.error.HTTPError as e:
            return {"url": url, "ok": False,
                    "note": "页面访问受限（HTTP %s，可能是反爬或需登录），未能读取正文" % e.code}
        except Exception as e:
            return {"url": url, "ok": False, "note": "页面读取失败（%s）" % (e or "网络错误")}

        m = re.search(r'<title[^>]*>(.*?)</title>', html, re.S)
        title = re.sub(r'\s+', ' ', m.group(1)).strip() if m else ""
        txt = re.sub(r'<script.*?</script>', ' ', html, flags=re.S)
        txt = re.sub(r'<style.*?</style>', ' ', txt, flags=re.S)
        txt = re.sub(r'<[^>]+>', ' ', txt)
        txt = re.sub(r'\s+', ' ', txt)

        hits = []
        seen = set()
        for kw in _PAGE_KWS:
            for mm in re.finditer(re.escape(kw), txt):
                s = max(0, mm.start() - 70)
                e = min(len(txt), mm.end() + 120)
                seg = txt[s:e].strip()
                if seg[:30] in seen:
                    continue
                seen.add(seg[:30])
                hits.append(seg)
                if len(hits) >= self.max_hits:
                    break
            if len(hits) >= self.max_hits:
                break

        body = " || ".join(hits)[:self.max_chars]
        cjk = len(re.findall(r"[\u4e00-\u9fff]", txt))
        if not body or len(txt) < 300 or cjk < 30:
            return {"url": url, "ok": False, "title": title,
                    "note": "页面为动态渲染，正文无法直接提取，建议点击链接访问核实"}
        return {"url": url, "ok": True, "title": title, "text": body}


# ============================================================
# 云端大模型研判（OpenAI 兼容接口）
# ============================================================
class LLMClient:
    """调用 OpenAI 兼容的 chat/completions 接口。"""

    def __init__(self, config, timeout=120):
        self.base_url = (config.get("base_url") or "").rstrip("/")
        self.api_key = config.get("api_key") or ""
        self.model = config.get("model") or ""
        self.timeout = timeout

    def chat(self, messages, max_tokens=3000, temperature=0.4):
        if not self.api_key:
            raise RuntimeError("API_KEY_MISSING")
        if not self.base_url or not self.model:
            raise RuntimeError("CONFIG_INCOMPLETE")
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
                "User-Agent": UA,
            },
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
            body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            if e.code in (401, 403):
                raise RuntimeError("API_KEY_INVALID::%s" % err_body)
            raise RuntimeError("MODEL_HTTP_%s::%s" % (e.code, err_body))
        except urllib.error.URLError as e:
            raise RuntimeError("NETWORK_FAIL::%s" % (e.reason or "无法连接模型服务"))
        except Exception as e:
            raise RuntimeError("MODEL_CALL_FAIL::%s" % e)
        try:
            return body["choices"][0]["message"]["content"].strip()
        except Exception:
            raise RuntimeError("MODEL_RESPONSE_INVALID::%s" % json.dumps(body)[:200])


def build_prompt(category, item, conditions, results, pages=None):
    """构造研判提示词：搜索到的真实信息 + 已抓取页面要点 + 用户自身条件。"""
    cond_lines = []
    for k, v in conditions.items():
        if v is not None and str(v).strip() != "":
            cond_lines.append("- %s：%s" % (k, v))
    if not cond_lines:
        cond_lines.append("- （用户未填写其他条件）")

    search_lines = []
    for i, r in enumerate(results, 1):
        snip = r["snippet"] or "（无摘要）"
        if len(snip) > 180:
            snip = snip[:180] + "…"
        search_lines.append("%d. %s\n   来源：%s\n   摘要：%s" % (i, r["title"], r["url"], snip))

    page_lines = []
    ok_pages = [p for p in (pages or []) if isinstance(p, dict) and p.get("ok")]
    for i, p in enumerate(ok_pages, 1):
        body = p.get("text") or ""
        if len(body) > 900:
            body = body[:900] + "…"
        page_lines.append("%d. 页面标题：%s\n   来源：%s\n   正文要点：%s" % (i, p.get("title") or "-", p.get("url"), body))
    if not page_lines:
        page_lines.append("（搜索结果页面多为动态渲染或访问受限，未能提取正文；请勿编造，缺省信息如实说明）")

    system = (
        "你是一名严谨、客观的个人消费决策分析师，擅长把模糊的购物冲动转化为理性的购买判断。"
        "你会基于搜索到的商品真实信息与用户自身条件进行多维度对比研判，绝不夸大、不编造数据。"
        "硬数据（价格、配置、参数）只允许引用【联网搜索结果】与【已抓取页面要点】中实际出现的内容；"
        "若其中没有价格/参数，必须明确写“公开搜索暂未收录该信息，建议以官方渠道核实”，"
        "不得凭空编造精确数字冒充实时数据。可用模型知识补充一般性背景（品牌定位、同类价位带等），"
        "但必须标注“（模型知识，非实时，需核实）”。信息不完整时如实说明，并建议用户通过官方渠道核实。"
    )
    user = (
        "【待决策物品】%s\n"
        "【商品品类】%s\n"
        "【用户自身条件】\n%s\n\n"
        "【联网搜索到的该商品真实信息】\n%s\n\n"
        "【已抓取页面正文要点】（来自搜索结果中较有信息量的页面）\n%s\n\n"
        "请严格按以下格式输出（第一行必须是结论行，格式不可变）：\n"
        "结论：可以购买 或 观望暂缓 或 不建议购买（三选一，仅选一个）\n"
        "\n"
        "然后输出一份结构化的中文分析报告，包含以下小节：\n"
        "## 一、决策结论\n对结论的一句话总述。\n"
        "## 二、商品关键信息（基于搜索结果与抓取内容）\n"
        "归纳搜索确认到的价格、核心配置、口碑等要点；明确标注哪些信息已确认、哪些为“待官方核实”或“模型知识（非实时）”，并注明来源。\n"
        "## 三、与你的条件匹配度分析\n逐条对照用户自身条件（预算、现用设备、刚需、替代方案等），分析是否匹配、能否承受。\n"
        "## 四、支持购买（正向因素）\n列出值得买的理由，用 - 列表。\n"
        "## 五、反对购买（风险因素）\n列出风险与顾虑（闲置、贬值、机会成本、替代方案等），用 - 列表。\n"
        "## 六、最终建议\n给出一条明确、可执行的行动建议（含观望期限、替代方案、官方核实渠道等）。\n"
        "报告语言使用简体中文，客观克制，不煽动。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user % (item, category, "\n".join(cond_lines),
                                            "\n".join(search_lines), "\n".join(page_lines))},
    ]


def parse_verdict(report):
    """从报告第一行解析结论。"""
    first = report.strip().splitlines()[0] if report.strip() else ""
    if "不建议购买" in first:
        return "no"
    if "可以购买" in first:
        return "buy"
    if "观望" in first:
        return "wait"
    return "wait"


# ============================================================
# 配置
# ============================================================
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ============================================================
# HTTP 服务
# ============================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "PurchaseDecisionTool/1.0"
    error_log_path = os.path.join(BASE_DIR, "error.log")

    def handle_one_request(self):
        """任何未捕获异常都落盘到 error.log，便于诊断。"""
        try:
            super().handle_one_request()
        except Exception:
            try:
                with open(self.error_log_path, "a", encoding="utf-8") as f:
                    f.write("\n[%s] %s\n%s\n" % (
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        self.requestline or "",
                        traceback.format_exc(),
                    ))
            except Exception:
                pass
            try:
                self.close_connection = True
            except Exception:
                pass

    def log_message(self, fmt, *args):
        # 窗口模式（--noconsole）下 sys.stderr 为 None，必须防护
        try:
            if sys.stderr is not None:
                sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
        except Exception:
            pass

    # ---- 工具 ----
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, msg, code=500):
        self._send_json({"ok": False, "error": msg}, code)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # ---- 路由 ----
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/ping":
            return self._send_json({"ok": True, "app": APP_NAME})

        if path == "/api/config":
            cfg = load_config()
            return self._send_json({"ok": True, "config": cfg, "providers": PROVIDERS})

        if path == "/api/search":
            qs = urllib.parse.parse_qs(parsed.query)
            q = (qs.get("q") or [""])[0].strip()
            suffix = (qs.get("suffix") or [""])[0].strip()
            try:
                n = min(int((qs.get("n") or ["8"])[0]), 10)
            except ValueError:
                n = 8
            if not q:
                return self._send_error_json("缺少搜索关键词", 400)
            try:
                bs = BingSearch()
                results = bs.search_multi(q, suffix, n)
                # 从结果中挑选“值得抓取”的详情页（打分高且非明显官网无关页），并行抓取正文
                fetch_candidates = [r for r in results if _rank_result(r["title"], r["url"]) >= 0][:4]
                pages = []
                if fetch_candidates:
                    with ThreadPoolExecutor(max_workers=3) as ex:
                        pages = list(ex.map(PageExtractor().extract,
                                            [r["url"] for r in fetch_candidates]))
                return self._send_json({"ok": True, "query": q, "results": results, "pages": pages})
            except RuntimeError as e:
                return self._send_error_json(str(e))
            except Exception as e:
                return self._send_error_json("搜索失败：%s" % e)

        if path == "/":
            path = "/index.html"
        # 静态资源
        if path.startswith("/web/"):
            path = path[len("/web/"):]
        rel = os.path.normpath(path.lstrip("/"))
        if rel.startswith("..") or os.path.isabs(rel):
            return self._send_error_json("非法路径", 403)
        fpath = os.path.join(WEB_DIR, rel)
        if not os.path.isfile(fpath):
            return self._send_error_json("资源不存在", 404)
        ext_map = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
        }
        ext = os.path.splitext(fpath)[1].lower()
        ctype = ext_map.get(ext, "application/octet-stream")
        try:
            with open(fpath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._send_error_json("读取资源失败：%s" % e)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/config":
            body = self._read_body()
            cfg = load_config()
            for k in ("provider", "base_url", "api_key", "model"):
                if k in body:
                    cfg[k] = str(body[k]).strip()
            try:
                save_config(cfg)
            except Exception as e:
                return self._send_error_json("配置保存失败：%s" % e)
            return self._send_json({"ok": True, "config": cfg})

        if path == "/api/test":
            """最小对话，用于测试模型服务连通性。"""
            cfg = load_config()
            try:
                client = LLMClient(cfg, timeout=60)
                reply = client.chat(
                    [{"role": "user", "content": "请只回复两个字：正常"}],
                    max_tokens=20,
                )
                return self._send_json({"ok": True, "reply": reply[:50], "model": cfg.get("model")})
            except RuntimeError as e:
                msg = str(e)
                if msg.startswith("API_KEY_MISSING"):
                    return self._send_error_json("未配置API Key，请先填写", 501)
                if msg.startswith("API_KEY_INVALID"):
                    return self._send_error_json("API Key 无效或已过期（服务返回 401/403）", 502)
                if msg.startswith("CONFIG_INCOMPLETE"):
                    return self._send_error_json("模型服务配置不完整，请填写接口地址与模型名称", 503)
                if msg.startswith("NETWORK_FAIL"):
                    return self._send_error_json("无法连接模型服务，请检查网络或接口地址", 504)
                if msg.startswith("MODEL_HTTP_"):
                    return self._send_error_json("模型服务返回错误：%s" % msg[len("MODEL_HTTP_"):], 505)
                return self._send_error_json("连接测试失败：%s" % msg, 506)
            except Exception as e:
                return self._send_error_json("连接测试失败：%s" % e)

        if path == "/api/analyze":
            body = self._read_body()
            item = str(body.get("item") or "").strip()
            category = str(body.get("category") or "其他").strip()
            conditions = body.get("conditions") or {}
            results = body.get("results") or []
            pages = body.get("pages") or []
            if not item:
                return self._send_error_json("缺少物品名称", 400)
            if not results:
                return self._send_error_json("缺少搜索结果", 400)
            cfg = load_config()
            try:
                client = LLMClient(cfg)
                messages = build_prompt(category, item, conditions, results, pages)
                report = client.chat(messages)
                verdict = parse_verdict(report)
                return self._send_json({
                    "ok": True,
                    "verdict": verdict,
                    "report": report,
                    "dataSource": "联网搜索 + 云端AI研判",
                    "model": cfg.get("model"),
                })
            except RuntimeError as e:
                msg = str(e)
                if msg.startswith("API_KEY_MISSING"):
                    return self._send_error_json("未配置API Key，请先在右上角「设置」中填写模型服务信息", 501)
                if msg.startswith("API_KEY_INVALID"):
                    return self._send_error_json("API Key 无效或已过期（服务返回 401/403），请检查设置", 502)
                if msg.startswith("CONFIG_INCOMPLETE"):
                    return self._send_error_json("模型服务配置不完整，请在设置中填写接口地址与模型名称", 503)
                if msg.startswith("NETWORK_FAIL"):
                    return self._send_error_json("无法连接模型服务，请检查网络或接口地址", 504)
                if msg.startswith("MODEL_HTTP_"):
                    return self._send_error_json("模型服务返回错误：%s" % msg[len("MODEL_HTTP_"):], 505)
                return self._send_error_json("AI研判失败：%s" % msg, 506)
            except Exception as e:
                return self._send_error_json("AI研判失败：%s" % e)

        return self._send_error_json("接口不存在", 404)


def pick_port(start=8765, tries=20):
    for p in range(start, start + tries):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            return p, srv
        except OSError:
            continue
    raise RuntimeError("无法找到可用端口")


def _fatal_box(title, msg):
    """EXE 窗口模式下，启动失败时弹出可见的错误提示。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)
    except Exception:
        sys.stderr.write("%s：%s\n" % (title, msg))


def main():
    # 控制台输出统一使用 UTF-8，避免 Windows 下中文乱码（窗口模式下 stdout/stderr 可能为 None）
    try:
        if sys.stdout is not None:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr is not None:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        port, srv = pick_port()
    except Exception as e:
        _fatal_box(APP_NAME, "启动失败，无法找到可用端口。\n\n%s" % e)
        return
    url = "http://127.0.0.1:%d" % port
    print("=" * 52)
    print("  %s 已启动" % APP_NAME)
    print("  访问地址：%s" % url)
    print("  数据全部保存在本机，关闭窗口或按 Ctrl+C 退出")
    print("=" * 52)

    def open_browser():
        time.sleep(1.0)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=open_browser, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    except Exception as e:
        _fatal_box(APP_NAME, "运行异常，工具已停止。\n\n%s" % e)
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
