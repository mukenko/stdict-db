#!/usr/bin/env python3
"""표준국어대사전 조회 코어 (data/stdict.sqlite 를 읽기 전용으로만 연다). 표준 라이브러리만 사용, Python 3.9 이상.

MCP/REST 어댑터는 이 모듈의 Dictionary 와 format_* 함수만 호출한다(어댑터에 조회 로직을 두지 않는다).

  python3 stdict_lookup.py 연기                      # 동음이의어 전부(표제어·어깨번호·원어·발음·뜻풀이)
  python3 stdict_lookup.py 연기 --examples --json    # 용례 포함, JSON 출력
  python3 stdict_lookup.py 연:기 --by pronunciation  # 발음으로 찾기 (ː 와 : 모두 가능, 장음 기호가 없으면 장단 모두)
  python3 stdict_lookup.py 演技 --by hanja           # 원어(한자)로 찾기
  python3 stdict_lookup.py 가계해 --by form          # 활용형·준말로 표제어 찾기
  python3 stdict_lookup.py "목질로 된 식물" --by definition   # 뜻풀이에서 찾기

DB 가 빌드로 교체(os.replace)되면 다음 요청에서 자동으로 다시 연결한다. 질의에는 시간 제한이 있다.
출전이 있는 용례도 그대로 보여 준다(출전은 ≪…≫ 로 덧붙인다).
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path

import stdict_stem

HERE = Path(__file__).resolve().parent
DEFAULT_DB = Path(os.environ.get("STDICT_DB") or HERE / "data" / "stdict.sqlite")
DEFAULT_TIMEOUT = 3.0                  # 한 질의가 이 시간(초)을 넘으면 중단
ATTRIBUTION = "출처: 국립국어원 표준국어대사전 (CC BY-SA 2.0 KR)"
LONG_NOTE = "발음의 ː 는 장음(길게 소리 냄)을 뜻함"
SEARCH_KINDS = ("pronunciation", "hanja", "form", "definition")

_SUP = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


class StdictError(Exception):
    """사용자에게 그대로 보여 줄 수 있는 오류 문장을 담는다."""


class DictUnavailable(StdictError):
    pass


class QueryTimeout(StdictError):
    pass


# ---------------------------------------------------------------- 입력 정규화
def normalize(text):
    """macOS 의 자모 분리(NFD) 입력을 NFC 로 맞추고, 표제어의 하이픈(-)·^ 와 겹공백을 없앤다."""
    text = unicodedata.normalize("NFC", text or "")
    return " ".join(re.sub(r"[-^]", "", text).split())


def word_key(text):
    """표제어 조회 키: 띄어쓰기까지 뺀다(어깨번호 그룹과 사전 검색이 이 기준으로 묶는다)."""
    return normalize(text).replace(" ", "")


def pron_key(text):
    """발음 검색용: DB 의 pron_norm 은 장음 ː 를 콜론(:)으로 적어 두었다."""
    return normalize(text).replace("ː", ":")


def sup(n):
    return str(n).translate(_SUP) if n else ""


def _like(text):
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _fts_phrase(text):
    return '"' + text.replace('"', '""') + '"'


def _cut(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _link_word(word):
    """관련어 표제어 끝의 어깨번호('계년02')를 위첨자로 바꿔 보여 준다."""
    m = re.match(r"^(.*?)(\d{2})$", word or "")
    if m and m.group(1):
        return normalize(m.group(1)) + sup(int(m.group(2)))
    return normalize(word or "")


# ---------------------------------------------------------------- 사전
class Dictionary:
    def __init__(self, path=None, timeout=DEFAULT_TIMEOUT):
        self.path = Path(path) if path else DEFAULT_DB
        self.timeout = timeout
        self._local = threading.local()
        self._all, self._lock = [], threading.Lock()      # 모든 스레드의 연결(close 용)

    def close(self):
        """열려 있는 모든 연결을 닫는다. 이후에 조회하면 다시 연다."""
        with self._lock:
            conns, self._all = self._all, []
        for c in conns:
            try:
                c.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    # -- 연결: 스레드마다 하나, 파일이 교체되면 다시 연다
    def _signature(self):
        try:
            st = os.stat(self.path)
        except OSError:
            raise DictUnavailable("사전 DB를 열 수 없습니다. 서버 관리자에게 알려 주세요.")
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def conn(self):
        sig = self._signature()
        c = getattr(self._local, "conn", None)
        if c is None or self._local.sig != sig:
            if c is not None:
                with self._lock:
                    if c in self._all:
                        self._all.remove(c)
                c.close()
            uri = "file:" + urllib.parse.quote(str(self.path)) + "?mode=ro"
            c = None
            try:
                c = sqlite3.connect(uri, uri=True, check_same_thread=False)   # 스레드마다 자기 연결만 쓴다
                c.row_factory = sqlite3.Row
                c.execute("PRAGMA query_only=ON")
                c.execute("SELECT 1 FROM meta LIMIT 1")
            except sqlite3.Error:
                if c is not None:
                    c.close()
                raise DictUnavailable("사전 DB를 열 수 없습니다. 서버 관리자에게 알려 주세요.")
            self._local.conn, self._local.sig = c, sig
            with self._lock:
                self._all.append(c)
        return c

    def q(self, sql, params=(), timeout=None):
        c = self.conn()
        deadline = time.monotonic() + (timeout or self.timeout)
        c.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 20000)
        try:
            return c.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e):
                raise QueryTimeout("검색이 너무 오래 걸려 중단했습니다. 검색어를 더 구체적으로 줄여 주세요.")
            raise
        finally:
            c.set_progress_handler(None, 0)

    def meta(self):
        return {r["key"]: r["value"] for r in self.q("SELECT key, value FROM meta")}

    # -- 표제어 조회
    def lookup(self, word, max_entries=12, include_examples=False, full=False):
        """표제어(하이픈·^ 무시)가 같은 항목(동음이의어)을 전부 찾는다. 없으면 활용형·비슷한 표제어를 제안한다.
        full=True 는 화면(원본 보기)용: 동음이의어·뜻·용례·관련어 개수 제한을 두지 않는다."""
        max_entries = 10000 if full else max(1, min(int(max_entries), 50))
        norm = normalize(word)
        if not norm:
            raise StdictError("조회할 단어를 입력해 주세요.")
        key = word_key(norm)
        codes = [r[0] for r in self.q(
            "SELECT target_code FROM entries WHERE word_key=? "
            "ORDER BY sup_no IS NULL, sup_no, target_code", (key,))]
        res = {"query": word, "normalized": norm, "total": len(codes), "shown": 0, "entries": [],
               "pron_groups": self._pron_groups(key) if len(codes) > 1 else [], "base_forms": [], "inferred": False,
               "suggestions": []}
        for code in codes[:max_entries]:
            res["entries"].append(self._entry(code, include_examples, full=full))
        res["shown"] = len(res["entries"])
        if not codes:
            codes, _, notes = self._find_form(norm, 8)
            res["base_forms"] = self._summaries(codes, notes=notes)
            res["inferred"] = any(n.startswith("규칙으로 추정") for n in notes.values())
            res["suggestions"] = self.suggest(norm)
        return res

    def _pron_groups(self, norm):
        """동음이의어 전체를 발음별로 묶는다(장음 ː 가 있는 묶음이 먼저). 발음이 하나뿐이면 빈 목록."""
        by_code = {}
        for r in self.q(
                "SELECT e.target_code, e.sup_no, e.original_text, p.pronunciation FROM entries e "
                "LEFT JOIN pronunciations p ON p.target_code=e.target_code WHERE e.word_key=? "
                "ORDER BY e.sup_no IS NULL, e.sup_no, e.target_code, p.seq", (norm,)):
            c = by_code.setdefault(r["target_code"], {"code": r["target_code"], "sup_no": r["sup_no"],
                                                       "original": r["original_text"], "prons": []})
            if r["pronunciation"] and r["pronunciation"] not in c["prons"]:
                c["prons"].append(r["pronunciation"])
        groups = {}
        for c in by_code.values():
            key = " / ".join(c["prons"]) or "(발음 정보 없음)"
            groups.setdefault(key, []).append({"target_code": c["code"], "sup_no": c["sup_no"], "original": c["original"]})
        if len(groups) < 2:
            return []
        out = [{"pronunciation": k, "entries": v} for k, v in groups.items()]
        out.sort(key=lambda g: "ː" not in g["pronunciation"])        # 장음 묶음을 먼저(sort 는 안정 정렬)
        return out

    def entry(self, code, include_examples=False, full=False):
        """항목 하나(동음이의어 하나)를 읽는다. full=True 는 개수 제한이 없다."""
        return self._entry(code, include_examples, full=full)

    def _entry(self, code, include_examples, max_senses=8, max_rel=6, full=False):
        if full:
            max_senses = max_rel = 10 ** 6
        e = self.q("SELECT * FROM entries WHERE target_code=?", (code,))[0]
        out = {
            "target_code": code, "word": e["word_plain"], "headword": e["word"], "sup_no": e["sup_no"],
            "unit": e["word_unit"], "type": e["word_type"], "original": e["original_text"],
            "origins": [{"text": r["original_language"], "language": r["language_type"]} for r in self.q(
                "SELECT original_language, language_type FROM origins WHERE target_code=? ORDER BY seq", (code,))],
            "pronunciations": [], "conjugations": [], "etymology": e["origin"], "allomorph": e["allomorph"],
            "norm_info": [{"type": r["type"], "desc": r["desc"]} for r in self.q(
                "SELECT type, desc FROM norm_info WHERE target_code=? ORDER BY seq", (code,))],
            "pos": [], "relations": [],
        }
        seen = set()
        for r in self.q("SELECT pronunciation FROM pronunciations WHERE target_code=? ORDER BY seq", (code,)):
            if r[0] and r[0] not in seen:
                seen.add(r[0])
                out["pronunciations"].append(r[0])
        for c in self.q("SELECT id, kind, form FROM conjugations WHERE target_code=? ORDER BY seq", (code,)):
            prons = [r[0] for r in self.q("SELECT pronunciation FROM conj_pron WHERE conjugation_id=? ORDER BY seq",
                                          (c["id"],)) if r[0]]
            out["conjugations"].append({"kind": c["kind"], "form": c["form"], "pronunciations": prons})
        out["relations"] = self._relations(
            "scope IN ('word','relation_info') AND scope_code=?", (code,), max_rel)
        for p in self.q("SELECT pos_code, pos FROM pos_entries WHERE target_code=? ORDER BY seq", (code,)):
            pos = {"pos": p["pos"], "patterns": [], "senses": [], "more_senses": 0}
            for cp in self.q("SELECT comm_pattern_code FROM patterns WHERE pos_code=? ORDER BY seq", (p["pos_code"],)):
                cpc = cp[0]
                pos["patterns"] += [r[0] for r in self.q(
                    "SELECT text FROM comm_patterns_text WHERE comm_pattern_code=? ORDER BY seq", (cpc,)) if r[0]]
                for s in self.q("SELECT sense_code, type, definition, scientific_name FROM senses "
                                "WHERE comm_pattern_code=? ORDER BY seq", (cpc,)):
                    if len(pos["senses"]) >= max_senses:
                        pos["more_senses"] += 1
                        continue
                    pos["senses"].append(self._sense(s, include_examples, max_rel, full))
            out["pos"].append(pos)
        return out

    def _sense(self, s, include_examples, max_rel, full=False):
        sc = s["sense_code"]
        sense = {
            "code": sc, "type": s["type"], "definition": s["definition"], "scientific_name": s["scientific_name"],
            "cats": [r[0] for r in self.q("SELECT cat FROM sense_cats WHERE sense_code=?", (sc,)) if r[0]],
            "patterns": [r[0] for r in self.q("SELECT text FROM sense_patterns WHERE sense_code=?", (sc,)) if r[0]],
            "examples": [], "example_sources": [], "relations": self._relations("scope='sense' AND scope_code=?", (sc,), max_rel),
        }
        if include_examples:
            rows = [r for r in self.q(
                f"SELECT example, source FROM examples WHERE sense_code=? ORDER BY seq LIMIT ?",
                (sc, -1 if full else 3)) if r[0]]
            sense["examples"] = [r[0] for r in rows]
            sense["example_sources"] = [r[1] for r in rows]       # 용례와 같은 순서(출전이 없으면 None)
        return sense

    def _relations(self, where, params, max_rel):
        groups, order = {}, []
        for r in self.q(f"SELECT rel_type, word FROM relations WHERE {where} ORDER BY seq", params):
            t = r["rel_type"] or "관련"
            if t not in groups:
                groups[t] = []
                order.append(t)
            groups[t].append(_link_word(r["word"]))
        return [{"type": t, "total": len(groups[t]), "words": groups[t][:max_rel]} for t in order]

    # -- 역검색
    def search(self, query, by, limit=10):
        """역검색. 결과의 hits 는 요약이며, 항목 전체는 entry(코드)로 읽는다."""
        if by not in SEARCH_KINDS:
            raise StdictError(f"by 는 {', '.join(SEARCH_KINDS)} 중 하나여야 합니다.")
        limit = max(1, min(int(limit), 50))
        norm = normalize(query)
        if not norm:
            raise StdictError("검색어를 입력해 주세요.")
        codes, total, notes = getattr(self, f"_find_{by}")(norm, limit)
        return {"query": query, "by": by, "total": total, "shown": len(codes),
                "hits": self._summaries(codes, notes=notes)}  # total 이 None 이면 전체 개수를 세지 않은 것

    ORDER = "e.word_plain, e.sup_no IS NULL, e.sup_no, e.target_code"

    def _page(self, where, params, limit, extra_order="", order_params=()):
        total = self.q(f"SELECT COUNT(*) FROM entries e WHERE {where}", params)[0][0]
        rows = self.q(f"SELECT e.target_code FROM entries e WHERE {where} ORDER BY {extra_order}{self.ORDER} LIMIT ?",
                      tuple(params) + tuple(order_params) + (limit,))
        return [r[0] for r in rows], total

    def _has_column(self, table, column):
        """빌드 v4.1 이전 DB 에는 pron_plain 이 없다. DB 파일이 바뀌면(교체) 다시 확인한다."""
        sig = self._signature()
        cache = self.__dict__.setdefault("_columns", {})
        if cache.get("sig") != sig:
            cache.clear()
            cache["sig"] = sig
        if (table, column) not in cache:
            cache[(table, column)] = any(r[1] == column for r in self.q(f"PRAGMA table_info({table})"))
        return cache[(table, column)]

    def _find_pronunciation(self, norm, limit):
        key = pron_key(norm)
        if ":" in key:                                       # 장음을 지정: 정확히 일치
            where, params = "e.target_code IN (SELECT target_code FROM pronunciations WHERE pron_norm=?)", (key,)
        else:                                                # 장음 기호가 없으면 장단 모두
            col = "pron_plain" if self._has_column("pronunciations", "pron_plain") else "replace(pron_norm, ':', '')"
            where = f"e.target_code IN (SELECT target_code FROM pronunciations WHERE {col}=?)"
            params = (key,)
        codes, total = self._page(where, params, limit)
        return codes, total, {}

    def _find_hanja(self, norm, limit):
        if len(norm) >= 3:                                   # 3글자 이상은 trigram FTS(original_text 열)로 부분 일치
            where, params = ("e.target_code IN (SELECT rowid FROM fts_words WHERE fts_words MATCH ?)",
                             ("original_text:" + _fts_phrase(norm),))
        else:                                                # 1~2글자는 trigram 으로 못 찾아 전체를 훑는다
            where, params = "e.original_text LIKE ? ESCAPE '\\'", (_like(norm),)
        codes, total = self._page(where, params, limit, "(e.original_text=?) DESC, length(e.original_text), ",
                                  (norm,))
        return codes, total, {}

    INFER_POS = ("동사", "형용사", "보조 동사", "보조 형용사")

    def infer_base(self, norm, limit=8):
        """사전에 활용형으로 실리지 않은 활용형(먹었다·갔다·들었다)에서 어미를 떼어 표제어 후보를 찾는다.
        (코드 목록, {코드: 설명}). 두 근거만 쓴다: ① 규칙 활용: 어간+다 가 동사·형용사 표제어로 실제 있음
        ② 불규칙 활용: 어간이 stem_alts(사전이 싣는 활용형에서 뽑은 불규칙 어간)에 있음. 근거를 설명에 적는다."""
        if " " in norm or len(norm) < 2:
            return [], {}
        cands = stdict_stem.candidates(norm)
        rules = {}
        for stem, rule in cands:
            rules.setdefault(stem + "다", rule)
        if not rules:
            return [], {}
        marks = ",".join("?" * len(rules))
        pos = ",".join("?" * len(self.INFER_POS))
        rows = self.q(f"SELECT e.target_code, e.word_key FROM entries e WHERE e.word_key IN ({marks}) "
                      f"AND EXISTS (SELECT 1 FROM pos_entries p WHERE p.target_code=e.target_code AND p.pos IN ({pos})) "
                      f"ORDER BY length(e.word_key) DESC, e.word_key, e.sup_no IS NULL, e.sup_no, e.target_code",
                      tuple(rules) + self.INFER_POS)
        found = [(r[0], "규칙으로 추정: " + rules[r[1]] + f" → 기본형 '{r[1]}'") for r in rows]
        if self._has_table("stem_alts"):
            stems = dict(cands)
            smarks = ",".join("?" * len(stems))
            for r in self.q(f"SELECT a.target_code, a.stem, a.source_form, e.word_key FROM stem_alts a "
                            f"JOIN entries e ON e.target_code=a.target_code WHERE a.stem IN ({smarks}) "
                            f"ORDER BY length(e.word_key) DESC, e.word_key, e.sup_no IS NULL, e.sup_no, e.target_code",
                            tuple(stems)):
                found.append((r[0], f"규칙으로 추정: {stems[r[1]]} → 기본형 '{r[3]}' "
                                    f"(불규칙 활용: 사전에 활용형 '{r[2]}'가 실려 있어 어간이 '{r[1]}'로 바뀌는 것으로 봄)"))
        codes, notes = [], {}
        for code, note in found:
            if code not in notes and len(codes) < limit:
                codes.append(code)
                notes[code] = note
        return codes, notes

    def _has_table(self, name):
        sig = self._signature()
        cache = self.__dict__.setdefault("_tables", {})
        if cache.get("sig") != sig:
            cache.clear()
            cache["sig"] = sig
        if name not in cache:
            cache[name] = bool(self.q("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))
        return cache[name]

    def _codes_by_form(self, norm, limit):
        where, params = "e.target_code IN (SELECT target_code FROM conjugations WHERE form=?)", (norm,)
        codes, total = self._page(where, params, limit)
        return codes, total

    def _find_form(self, norm, limit):
        codes, total = self._codes_by_form(norm, limit)
        notes = {}
        for code in codes:
            kinds = [r[0] for r in self.q("SELECT DISTINCT kind FROM conjugations WHERE target_code=? AND form=?",
                                          (code, norm))]
            notes[code] = f"{'·'.join(kinds)}형 '{norm}'"
        if not codes:
            codes, notes = self.infer_base(norm, limit)
            total = len(codes) or 0
        return codes, total, notes

    def _find_definition(self, norm, limit):
        if len(norm) < 2:
            raise StdictError("뜻풀이 검색어는 2글자 이상이어야 합니다. 한 글자는 결과가 너무 많아 검색하지 않습니다. "
                              "그 글자가 단어라면 lookup_word 로 표제어를 조회하고, 발음·한자·활용형으로 찾으려면 "
                              "search_dictionary 의 by=pronunciation / hanja / form 을 쓰세요.")
        base = ("FROM senses s JOIN patterns p ON p.comm_pattern_code=s.comm_pattern_code "
                "JOIN pos_entries pe ON pe.pos_code=p.pos_code JOIN entries e ON e.target_code=pe.target_code WHERE ")
        if len(norm) >= 3:                                   # FTS5 trigram 은 3글자 미만이 안 된다
            join = base + "s.sense_code IN (SELECT rowid FROM fts_defs WHERE fts_defs MATCH ?)"
            params = (_fts_phrase(norm),)
            total = self.q(f"SELECT COUNT(DISTINCT e.target_code) {join}", params)[0][0]
            rows = self.q(f"SELECT e.target_code, MIN(s.sense_code) sc, s.definition d {join} "
                          f"GROUP BY e.target_code ORDER BY {self.ORDER} LIMIT ?", params + (limit,))
            return [r[0] for r in rows], total, {r[0]: _cut(r["d"], 80) for r in rows}
        # 2글자: 인덱스를 쓸 수 없어 전체를 훑어야 하므로(이 맥 0.4초, 파이는 몇 배) 한 번만 훑고 앞쪽 일부만 돌려준다.
        # 정렬·전체 개수를 포기하는 대신 흔한 말은 바로 끝난다.
        params = (_like(norm), limit * 4)
        rows = self.q(f"SELECT e.target_code, s.definition d {base}s.definition LIKE ? ESCAPE '\\' LIMIT ?", params)
        codes, notes = [], {}
        for r in rows:
            if r[0] not in notes:
                codes.append(r[0])
                notes[r[0]] = _cut(r["d"], 80)
        codes = codes[:limit]
        return codes, None, {c: notes[c] for c in codes}

    def suggest(self, norm, limit=6):
        """표제어가 없을 때 같은 글자로 시작하는(또는 3글자 이상이면 포함하는) 표제어를 제안한다."""
        words = [r[0] for r in self.q(
            "SELECT DISTINCT word_plain FROM entries WHERE word_plain>=? AND word_plain<? ORDER BY word_plain LIMIT ?",
            (norm, norm + "￿", limit))]
        if len(words) < limit and len(norm) >= 3:
            for r in self.q("SELECT DISTINCT e.word_plain FROM fts_words f JOIN entries e ON e.target_code=f.rowid "
                            "WHERE fts_words MATCH ? ORDER BY length(e.word_plain), e.word_plain LIMIT ?",
                            ("word_plain:" + _fts_phrase(norm), limit)):
                if r[0] not in words and len(words) < limit:
                    words.append(r[0])
        return words

    def _summaries(self, codes, note=None, notes=None):
        out = []
        for code in codes:
            e = self.q("SELECT word_plain, sup_no, word_unit, word_type, original_text FROM entries "
                       "WHERE target_code=?", (code,))[0]
            prons, seen = [], set()
            for r in self.q("SELECT pronunciation FROM pronunciations WHERE target_code=? ORDER BY seq", (code,)):
                if r[0] and r[0] not in seen:
                    seen.add(r[0])
                    prons.append(r[0])
            first = self.q("SELECT pe.pos, s.definition FROM pos_entries pe JOIN patterns p ON p.pos_code=pe.pos_code "
                           "JOIN senses s ON s.comm_pattern_code=p.comm_pattern_code WHERE pe.target_code=? "
                           "ORDER BY pe.seq, p.seq, s.seq LIMIT 1", (code,))
            out.append({"target_code": code, "word": e["word_plain"], "sup_no": e["sup_no"], "original": e["original_text"],
                        "pronunciations": prons, "pos": first[0]["pos"] if first else None,
                        "definition": _cut(first[0]["definition"], 80) if first else None,
                        "match": (notes or {}).get(code) or note})
        return out


# ---------------------------------------------------------------- 사람·LLM 이 읽는 글
def _head(e):
    prons = " / ".join(e["pronunciations"])
    orig = f" ({e['original']})" if e["original"] else ""
    kind = "·".join(x for x in (e["unit"], e["type"]) if x)
    return f"■ {e['word']}{sup(e['sup_no'])}{orig}" + (f" [{prons}]" if prons else "") + (f" {kind}" if kind else "")


def _conj_text(c):
    """활용형과 그 발음: '제작만[제ː장만]'."""
    return c["form"] + (f"[{' / '.join(c['pronunciations'])}]" if c["pronunciations"] else "")


def _rel_line(rels, indent):
    parts = []
    for r in rels:
        more = f" 외 {r['total'] - len(r['words'])}개" if r["total"] > len(r["words"]) else ""
        parts.append(f"{r['type']}: {', '.join(r['words'])}{more}")
    return [f"{indent}({' / '.join(parts)})"] if parts else []


def _ranges(nums):
    """[1,2,3,4,6,7,8,10] -> '1~4, 6~8, 10'. 위첨자를 이어 쓰면 작은 모델이 숫자 경계를 못 읽는다."""
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append((start, prev))
            start = prev = n
    if start is not None:
        out.append((start, prev))
    return ", ".join(str(a) if a == b else f"{a}~{b}" for a, b in out)


def _pron_items(entries, explicit):
    """묶음의 항목 표시. 작은 묶음은 '5번(捐棄)'처럼 하나씩, 큰 묶음은 번호 범위 + 한자 나열."""
    numbered = [x for x in entries if x["sup_no"]]
    if explicit or len(numbered) != len(entries):
        return ", ".join((f"{x['sup_no']}번" if x["sup_no"] else "번호 없음") + (f"({x['original']})" if x["original"] else "")
                         for x in entries)
    origs = ", ".join(x["original"] for x in entries if x["original"])
    return f"어깨번호 {_ranges([x['sup_no'] for x in entries])}" + (f" ({origs})" if origs else "")


def _pron_summary(res):
    lines = [f"발음별 요약(동음이의어 전체 {res['total']}개 기준. 아래 목록과 함께 읽고, 여기 없는 것은 덧붙이지 말 것):"]
    long_items, long_n = [], 0
    for g in res["pron_groups"]:
        has_long = "ː" in g["pronunciation"]
        small = len(g["entries"]) <= 4
        mark = " 장음 포함" if has_long else ""
        lines.append(f"  [{g['pronunciation']}]{mark} {len(g['entries'])}개: {_pron_items(g['entries'], small)}")
        if has_long:
            long_n += len(g["entries"])
            long_items.append(_pron_items(g["entries"], True))
    if long_n and long_n < res["total"]:
        lines.append(f"  ※ 장음(ː)이 있는 것은 {long_n}개({'; '.join(long_items)})이고, "
                     f"나머지 {res['total'] - long_n}개는 장음이 없다.")
    return lines


def format_lookup(res):
    lines = []
    q = res["normalized"]
    if not res["total"]:
        lines.append(f"「{q}」은(는) 표제어에 없습니다.")
        if res.get("inferred"):
            lines.append("  사전에 활용형으로 실려 있지는 않지만, 어미를 떼어 규칙으로 추정한 기본형 후보 "
                         "(여기 없는 불규칙 활용이면 다른 표제어일 수 있음):")
        for b in res["base_forms"]:
            lines.append("  " + ("추정 기본형: " if res.get("inferred") else "활용형으로는 있음: ") + _hit_line(b, "form"))
        if res["suggestions"]:
            lines.append("  비슷한 표제어: " + ", ".join(res["suggestions"]))
        lines.append(ATTRIBUTION)
        return "\n".join(lines)
    more = f" 중 {res['shown']}개" if res["shown"] < res["total"] else ""
    lines.append(f"「{q}」 동음이의어 {res['total']}개{more} ({LONG_NOTE})")
    if res["pron_groups"]:
        lines += _pron_summary(res)
    for e in res["entries"]:
        lines.append(_head(e))
        if e["allomorph"]:
            lines.append(f"  이형태: {_cut(e['allomorph'], 120)}")
        if e["conjugations"]:
            by = {}
            for c in e["conjugations"]:
                by.setdefault(c["kind"], []).append(_conj_text(c))
            lines.append("  " + "; ".join(f"{k}: {', '.join(v)}" for k, v in by.items()))
        if e["etymology"]:
            lines.append(f"  어원: {_cut(e['etymology'], 100)}")
        for n in e["norm_info"][:2]:
            lines.append(f"  규범({n['type']}): {_cut(n['desc'], 160)}")
        lines += _rel_line(e["relations"], "  ")
        for p in e["pos"]:
            lines.append(f"  〔{p['pos']}〕" + (f" 문형: {', '.join(p['patterns'][:4])}" if p["patterns"] else ""))
            for i, s in enumerate(p["senses"], 1):
                cat = "".join(f"『{c}』" for c in s["cats"])
                typ = f"<{s['type']}> " if s["type"] and s["type"] != "일반어" else ""
                sci = f" (학명: {s['scientific_name']})" if s["scientific_name"] else ""
                lines.append(f"   {i}. {typ}{cat + ' ' if cat else ''}{_cut(s['definition'], 300)}{sci}")
                for ex, src in zip(s["examples"], s["example_sources"]):
                    lines.append(f"      용례: {_cut(ex, 120)}" + (f" ≪{src}≫" if src else ""))
                lines += _rel_line(s["relations"], "      ")
            if p["more_senses"]:
                lines.append(f"   … 뜻 {p['more_senses']}개 더 있음")
    if res["shown"] < res["total"]:
        lines.append(f"(동음이의어 {res['total'] - res['shown']}개는 생략됨. max_entries 를 늘리면 더 볼 수 있음)")
    lines.append(ATTRIBUTION)
    return "\n".join(lines)


def _hit_line(h, by):
    orig = f" ({h['original']})" if h["original"] else ""
    prons = f" [{' / '.join(h['pronunciations'])}]" if h["pronunciations"] else ""
    return (f"{h['word']}{sup(h['sup_no'])}{orig}{prons}" + (f" 〔{h['pos']}〕" if h["pos"] else "")
            + (f" ← {h['match']}" if h["match"] and by == "form" else "")
            + (f" — {h['match']}" if h["match"] and by == "definition" else "")
            + (f" — {h['definition']}" if h["definition"] and by != "definition" else ""))


def format_search(res):
    names = {"pronunciation": "발음", "hanja": "원어", "form": "활용형", "definition": "뜻풀이"}
    note = f" ({LONG_NOTE})" if res["by"] == "pronunciation" else ""
    if res["total"] is None:
        count = f"앞쪽 {res['shown']}개만 표시(2글자 검색은 전체 개수를 세지 않음, 3글자 이상이면 정확히 검색)"
    else:
        count = f"{res['total']}개" + (f" 중 {res['shown']}개" if res["shown"] < res["total"] else "")
    lines = [f"{names[res['by']]} 검색 「{normalize(res['query'])}」: {count}{note}"]
    if not res["hits"]:
        lines.append("  결과 없음")
    for h in res["hits"]:
        lines.append("- " + _hit_line(h, res["by"]))
    lines.append(ATTRIBUTION)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", help="조회할 단어 또는 검색어")
    ap.add_argument("--by", choices=SEARCH_KINDS, help="역검색 종류(생략하면 표제어 조회)")
    ap.add_argument("--limit", type=int, default=12, help="보여 줄 최대 개수(기본 12)")
    ap.add_argument("--examples", action="store_true", help="용례 포함")
    ap.add_argument("--json", action="store_true", help="JSON 으로 출력")
    ap.add_argument("--db", help="DB 경로(기본 data/stdict.sqlite 또는 환경변수 STDICT_DB)")
    args = ap.parse_args()
    d = Dictionary(args.db)
    try:
        if args.by:
            res = d.search(args.query, args.by, args.limit)
            text = format_search(res)
        else:
            res = d.lookup(args.query, args.limit, args.examples)
            text = format_lookup(res)
    except StdictError as e:
        sys.exit(str(e))
    print(json.dumps(res, ensure_ascii=False, indent=1) if args.json else text)


if __name__ == "__main__":
    main()
