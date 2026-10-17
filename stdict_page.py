"""조회 결과를 사전 항목처럼 구조를 살려 그리는 HTML(문서형). 표준 라이브러리만 사용.

stdict_view.py 의 링크 서버가 '원본 보기'를 열 때 쓴다. 조회 코어(stdict_lookup.Dictionary)로 DB 에서 항목별로 읽어(full=True: 동음이의어·뜻·용례·
관련어를 줄이지 않는다) 다음처럼 그린다.

  - 맨 위: 발음별 요약(동음이의어가 발음이 둘 이상일 때), 동음이의어가 여럿이면 바로가기 목차.
  - 항목마다: 표제어+어깨번호, 원어, 발음, 단어/구·고유어/한자어, 오른쪽 위 "표준국어대사전 ↗"(누리집의 그 항목 하나).
  - 품사별 문형, 뜻풀이(번호), 용례(출전이 있는 것은 ≪출전≫ 표시), 관련어, 활용, 어원·규범 정보.

글꼴은 일반 시스템 글꼴이다. 글자는 DB 에서 읽은 그대로이고 HTML 로만 이스케이프한다(모델이 다시 쓰지 않는다).
"""
import html
import os
from pathlib import Path

import stdict_lookup as sl

SITE_VIEW = "https://stdict.korean.go.kr/search/searchView.do"
# 옛한글(어원의 옛 글자)은 시스템 글꼴로는 깨진다: 나눔바른고딕 옛한글판을 서버가 /f/font.otf 로 한 번만 보내 웹 글꼴로 쓴다
# (Safari 는 설치된 글꼴을 웹 페이지가 못 쓰게 막으므로 설치만으로는 안 된다). 글꼴이 없으면 일반 시스템 글꼴이다.
FONT_FILE = "NanumBarunGothic-YetHangul.otf"
FONT_DIRS = (Path.home() / "Library" / "Fonts", Path("/Library/Fonts"), Path("/System/Library/Fonts/Supplemental"))
FONT_URL = "/f/font.otf"
FONT_STACK = ('"StdictYetHangul", -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans CJK KR", '
              '"Noto Sans KR", "Segoe UI", sans-serif')
NAMES = {"pronunciation": "발음", "hanja": "원어", "form": "활용형", "definition": "뜻풀이"}


class Miss(Exception):
    """보여 줄 항목이 없다(조건에 맞는 표제어가 이제 없음)."""


def find_font():
    """바른고딕 옛한글판 글꼴 파일의 경로(없거나 STDICT_VIEW_FONT=0 이면 None). 설치된 것을 읽을 뿐이고 저장소에는 글꼴이 없다.
    STDICT_FONT_DIR 로 찾는 폴더를 바꿀 수 있다."""
    if os.environ.get("STDICT_VIEW_FONT", "1").strip().lower() in ("0", "off", "false", "no"):
        return None
    dirs = [Path(os.environ["STDICT_FONT_DIR"])] if os.environ.get("STDICT_FONT_DIR") else list(FONT_DIRS)
    for d in dirs:
        try:
            if (d / FONT_FILE).is_file():
                return d / FONT_FILE
        except OSError:
            continue
    return None


def original_url(target_code):
    """국립국어원 누리집에서 그 항목 하나를 여는 주소(word_no = target_code)."""
    return f"{SITE_VIEW}?word_no={int(target_code)}&searchKeywordTo=3"


def esc(text):
    return html.escape(text or "", quote=False)


def _attr(text):
    return html.escape(text or "", quote=True)


PHRASE_TYPES = ("속담", "관용구")        # 항목 전체가 따로 있는 관계: 화면에는 그 뜻풀이까지 보여 준다


def _word_link(word, code):
    """관련어. 그 항목이 DB 에 있으면 이 서버의 항목 화면(/e/코드)으로 가는 링크."""
    return f'<a href="/e/{int(code)}">{esc(word)}</a>' if code else esc(word)


def _rels(rels, skip=()):
    out = []
    for r in rels:
        if r["type"] in skip:
            continue
        codes = r.get("codes") or [None] * len(r["words"])
        words = ", ".join(_word_link(w, c) for w, c in zip(r["words"], codes))
        out.append(f'<span class="rel"><b>{esc(r["type"])}</b> {words}</span>')
    return f'<div class="rels">{"".join(out)}</div>' if out else ""


def phrase_codes(e):
    """항목의 속담·관용구(관계) 코드들."""
    return [c for r in e["relations"] if r["type"] in PHRASE_TYPES for c in (r.get("codes") or []) if c]


def _phrase_html(p):
    senses = "".join(_sense_html(i, s) for pos in p["pos"] for i, s in enumerate(pos["senses"], 1))
    return (f'<div class="phrase"><div class="ph-head"><span class="tag">{esc(p["unit"] or "")}</span> '
            f'<a href="/e/{p["target_code"]}">{esc(p["word"])}</a></div><ol class="senses">{senses}</ol></div>')


def phrases_html(e, phrases):
    items = [_phrase_html(phrases[c]) for c in phrase_codes(e) if c in phrases]
    return f'<section class="phrases"><h3>관용구/속담</h3>{"".join(items)}</section>' if items else ""


def _sense_html(i, s):
    cats = "".join(f'<span class="cat">{esc(c)}</span>' for c in s["cats"])
    typ = f'<span class="typ">{esc(s["type"])}</span>' if s["type"] and s["type"] != "일반어" else ""
    sci = f' <span class="sci">(학명: {esc(s["scientific_name"])})</span>' if s["scientific_name"] else ""
    pats = f'<div class="pat">문형: {esc(", ".join(s["patterns"]))}</div>' if s["patterns"] else ""
    cites = s.get("example_sources") or [None] * len(s["examples"])
    exs = "".join(f'<li>{esc(x)}{f" <cite>≪{esc(c)}≫</cite>" if c else ""}</li>' for x, c in zip(s["examples"], cites))
    exs = f'<ul class="ex">{exs}</ul>' if exs else ""
    return (f'<li value="{i}">{typ}{cats} {esc(s["definition"])}{sci}{pats}{exs}{_rels(s["relations"])}</li>')


def _pos_html(p):
    pats = f'<span class="pat">문형: {esc(", ".join(p["patterns"]))}</span>' if p["patterns"] else ""
    senses = "".join(_sense_html(i, s) for i, s in enumerate(p["senses"], 1))
    return f'<section class="pos"><h3>〔{esc(p["pos"])}〕 {pats}</h3><ol class="senses">{senses}</ol></section>'


def entry_html(e, note=None, phrases=None):
    sup = sl.sup(e["sup_no"])
    head = f'{esc(e["word"])}{sup}'
    orig = f'<span class="orig">({esc(e["original"])})</span>' if e["original"] else ""
    prons = " / ".join(e["pronunciations"])
    pron = f'<span class="pron">[{esc(prons)}]</span>' if prons else ""
    kind = "·".join(x for x in (e["unit"], e["type"]) if x)
    kind = f'<span class="meta">{esc(kind)}</span>' if kind else ""
    button = (f'<a class="site" href="{_attr(original_url(e["target_code"]))}" target="_blank" '
              f'rel="noopener noreferrer">표준국어대사전 ↗</a>')
    parts = [f'<article id="e{e["target_code"]}"><header><h2>{head}</h2>{orig}{pron}{kind}{button}</header>']
    if note:
        parts.append(f'<p class="note">{esc(note)}</p>')
    if e["allomorph"]:
        parts.append(f'<p class="note"><b>이형태</b> {esc(e["allomorph"])}</p>')
    if e["conjugations"]:
        by = {}
        for c in e["conjugations"]:
            by.setdefault(c["kind"], []).append(sl._conj_text(c))
        parts.append('<p class="note">' + " · ".join(f'<b>{esc(k)}</b> {esc(", ".join(v))}' for k, v in by.items()) + "</p>")
    if e["etymology"]:
        parts.append(f'<p class="note"><b>어원</b> {esc(e["etymology"])}</p>')
    for n in e["norm_info"]:
        parts.append(f'<p class="note"><b>규범({esc(n["type"])})</b> {esc(n["desc"])}</p>')
    parts.append(_rels(e["relations"], skip=PHRASE_TYPES if phrases else ()))
    parts.extend(_pos_html(p) for p in e["pos"])
    if phrases:
        parts.append(phrases_html(e, phrases))
    parts.append("</article>")
    return "".join(parts)


def toc_html(entries):
    if len(entries) < 2:
        return ""
    items = []
    for e in entries:
        orig = f' ({esc(e["original"])})' if e["original"] else ""
        prons = " / ".join(e["pronunciations"])
        items.append(f'<li><a href="#e{e["target_code"]}">{esc(e["word"])}{sl.sup(e["sup_no"])}{orig}'
                     f'{f" [{esc(prons)}]" if prons else ""}</a></li>')
    return f'<nav class="toc"><ol>{"".join(items)}</ol></nav>'


def hyphen_html(res):
    """표제어가 '<표제어>-' 로 시작하는 표제어(제작-하다, 제작-되다 …). 각각 이 서버의 그 항목 화면(/e/<코드>)으로 가는 링크."""
    if not res.get("hyphen_words"):
        return ""
    items = "".join(f'<li><a href="/e/{int(w["target_code"])}">{esc(w["word"])}{sl.sup(w["sup_no"])}</a>' + (f' <span class="orig">({esc(w["original"])})</span>' if w["original"] else "")
                    + "</li>" for w in res["hyphen_words"])
    return (f'<section class="hyphen"><h3>「{esc(res["normalized"].replace(" ", ""))}-」 형태로 시작하는 표제어 '
            f'<span class="meta">{res["hyphen_total"]}개</span></h3><ul>{items}</ul></section>')


def pron_summary_html(res):
    if not res["pron_groups"]:
        return ""
    rows = []
    for g in res["pron_groups"]:
        mark = " <b>장음 포함</b>" if "ː" in g["pronunciation"] else ""
        rows.append(f'<li><span class="pron">[{esc(g["pronunciation"])}]</span>{mark} {len(g["entries"])}개: '
                    f'{esc(sl._pron_items(g["entries"], len(g["entries"]) <= 4))}</li>')
    return (f'<section class="summary"><h3>발음별 요약</h3><ul>{"".join(rows)}</ul></section>')


STYLE = """
  :root { color-scheme: light dark; --bg:#fff; --fg:#1f2328; --mute:#6a737d; --line:#e1e4e8; --soft:#f6f8fa; --accent:#0b5cad; --hl:#fff3bf; }
  @media (prefers-color-scheme: dark) { :root { --bg:#16181d; --fg:#e6e8eb; --mute:#9aa4af; --line:#2c313a; --soft:#1e2229; --accent:#7db5ff; --hl:#4a3f12; } }
  * { box-sizing: border-box; }
  body { margin: 0 auto; max-width: 46rem; padding: 1.4rem 1.1rem 3rem; background: var(--bg); color: var(--fg);
         font: 17px/1.7 __STACK__; }
  .site-name { color: var(--mute); font-size: 13px; letter-spacing: .04em; }
  h1 { font-size: 1.1rem; margin: .2rem 0 1rem; color: var(--mute); font-weight: 600; }
  article { margin: 1.4rem 0; padding-top: 1rem; border-top: 1px solid var(--line); }
  header { display: flex; flex-wrap: wrap; align-items: baseline; gap: .3rem .7rem; }
  h2 { font-size: 1.7rem; line-height: 1.3; margin: 0; }
  h2 sup { font-size: .55em; color: var(--accent); }
  h3 { font-size: .95rem; margin: .9rem 0 .2rem; }
  .orig { color: var(--mute); }
  .pron { font-weight: 600; }
  .meta { border: 1px solid var(--line); border-radius: 999px; padding: 0 .6rem; font-size: 12px; line-height: 1.6; color: var(--mute); font-weight: 400; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  a.site { margin-left: auto; font-size: 12px; color: var(--mute); border: 1px solid var(--line); border-radius: 999px; padding: .05rem .75rem; white-space: nowrap; }
  a.site:hover { color: var(--accent); border-color: var(--accent); text-decoration: none; }
  .note { margin: .4rem 0; color: var(--mute); font-size: 15px; }
  .note b, .rel b { font-size: 12px; border: 1px solid var(--line); border-radius: .3rem; padding: 0 .35rem; margin-right: .3rem; font-weight: 500; }
  ol.senses { margin: .2rem 0; padding-left: 1.6rem; }
  ol.senses > li { margin: .45rem 0; padding-left: .2rem; }
  .cat { font-size: 12px; color: var(--accent); margin-right: .25rem; }
  .typ { font-size: 12px; color: var(--mute); border: 1px solid var(--line); border-radius: .3rem; padding: 0 .35rem; margin-right: .3rem; }
  .sci, .pat { color: var(--mute); font-size: 14px; }
  cite { font-style: normal; font-size: 13px; }
  ul.ex { margin: .2rem 0; padding-left: 1.2rem; color: var(--mute); font-size: 15px; }
  .rels { margin: .2rem 0; color: var(--mute); font-size: 14px; display: flex; flex-wrap: wrap; gap: .1rem .9rem; }
  nav.toc, section.summary { margin: 1rem 0; padding: .55rem .9rem; background: var(--soft); border: 1px solid var(--line); border-radius: .6rem; }
  nav.toc ol, section.summary ul { list-style: none; margin: 0; padding: 0; }
  nav.toc li, section.summary li { padding: .12rem 0; }
  section.summary h3 { margin-top: .2rem; }
  section.phrases { margin-top: 1.1rem; }
  .phrase { margin: .5rem 0 .9rem; padding-left: .8rem; border-left: 3px solid var(--line); }
  .ph-head { font-weight: 600; }
  .ph-head .tag { font-size: 12px; font-weight: 500; color: var(--mute); border: 1px solid var(--line); border-radius: .3rem; padding: 0 .4rem; margin-right: .3rem; }
  section.hyphen { margin-top: 1.6rem; padding-top: 1rem; border-top: 1px solid var(--line); }
  section.hyphen ul { list-style: none; margin: .3rem 0; padding: 0; display: flex; flex-wrap: wrap; gap: .2rem 1.1rem; }
  .lead { color: var(--mute); font-size: 14px; }
  footer { margin-top: 2.2rem; padding-top: .8rem; border-top: 1px solid var(--line); font-size: 13px; color: var(--mute); }
"""


def _page(title, body, subtitle=""):
    face = (f"  @font-face {{ font-family: 'StdictYetHangul'; src: url({FONT_URL}) format('opentype'); }}\n"
            if find_font() else "")
    css = face + STYLE.replace("__STACK__", FONT_STACK)
    sub = f'<p class="lead">{esc(subtitle)}</p>' if subtitle else ""
    return (f'<!doctype html>\n<html lang="ko">\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>{esc(title)} — 표준국어대사전</title>\n<style>{css}</style>\n'
            f'<div class="site-name">표준국어대사전</div>\n<h1>{esc(title)}</h1>\n{sub}{body}\n'
            f'<footer>{esc(sl.ATTRIBUTION)}</footer>\n')


def load_phrases(d, entries):
    """화면에 그릴 항목들의 속담·관용구 항목을 읽는다({코드: 항목}). DB 에 없는 코드는 건너뛴다."""
    out = {}
    for e in entries:
        for c in phrase_codes(e):
            if c not in out and d.q("SELECT 1 FROM entries WHERE target_code=?", (c,)):
                out[c] = d.entry(c, True, full=True)
    return out


def render_lookup(d, word, max_entries=12, include_examples=False):
    """표제어 조회 화면. 동음이의어·용례를 줄이지 않는다(max_entries·include_examples 는 채팅 결과의 조건이며 링크 키에만 쓴다).
    표제어가 없으면 Miss."""
    res = d.lookup(word, max_entries, True, full=True)
    if not res["total"]:
        raise Miss(word)
    entries = res["entries"]
    ph = load_phrases(d, entries)
    body = pron_summary_html(res) + toc_html(entries) + "".join(entry_html(e, phrases=ph) for e in entries) + hyphen_html(res)
    return _page(res["normalized"], body, f"동음이의어 {res['total']}개 ({sl.LONG_NOTE})")


def render_search(d, query, by, limit=10):
    """역검색 화면: 결과 항목을 모두 문서형으로. 결과가 없으면 Miss."""
    res = d.search(query, by, limit)
    if not res["hits"]:
        raise Miss(query)
    entries, notes = [], {}
    for h in res["hits"]:
        entries.append(d.entry(h["target_code"], True, full=True))
        if h["match"]:
            notes[h["target_code"]] = h["match"]
    total = res["total"]
    count = (f"앞쪽 {res['shown']}개만 표시" if total is None
             else f"{total}개" + (f" 중 {res['shown']}개" if res["shown"] < total else ""))
    note = f" ({sl.LONG_NOTE})" if by == "pronunciation" else ""
    ph = load_phrases(d, entries)
    body = toc_html(entries) + "".join(entry_html(e, notes.get(e["target_code"]), ph) for e in entries)
    return _page(f"{NAMES[by]} 검색 「{sl.normalize(query)}」", body, f"{count}{note}")


def render_entry(d, code):
    """항목 하나(target_code)의 화면. 없으면 Miss."""
    if not d.q("SELECT 1 FROM entries WHERE target_code=?", (code,)):
        raise Miss(code)
    e = d.entry(code, True, full=True)
    return _page(e["word"] + sl.sup(e["sup_no"]), entry_html(e, phrases=load_phrases(d, [e])))


def render_spec(d, spec):
    """링크에 등록된 검색 조건(spec)으로 화면을 그린다."""
    if spec["kind"] == "entry":
        return render_entry(d, spec["code"])
    if spec["kind"] == "lookup":
        return render_lookup(d, spec["q"], spec["max_entries"], spec["examples"])
    return render_search(d, spec["q"], spec["by"], spec["limit"])
