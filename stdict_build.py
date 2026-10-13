#!/usr/bin/env python3
"""표준국어대사전 조회용 DB 빌드: 원본(data/raw.sqlite) -> 조회용(data/stdict.sqlite). 표준 라이브러리만 사용.

  python3 stdict_build.py                      # 전체 빌드 -> 검증 -> 통과하면 교체(이전 버전 1개 보관)
  python3 stdict_build.py --limit 3000         # 앞의 3,000건만 빌드해 파서를 시험(교체하지 않음)
  python3 stdict_build.py --require-supno      # 어깨번호가 비는 중복 표제어가 있으면 실패로 처리

흐름: 원본 XML 파싱 -> 임시 DB(~/.cache/stdict-db, rsync 되는 폴더 밖) -> 인덱스·FTS5 -> 검증 -> 원자적 교체.
검증을 하나라도 못 넘기면 조회 중인 DB(data/stdict.sqlite)는 건드리지 않는다.
원본은 읽기 전용으로만 연다. API 호출도 인증키도 쓰지 않는다.

파서는 명세가 아니라 실제 수집분(stdict_fetch.py stats, 2026-10-12 437,018건) 기준이다.
  - 명세에 없는 norm_info 를 읽고, 제목 태그는 명세의 label 이 아니라 원본의 lable(오타)도 읽는다.
  - TREE 에 없는 태그가 나오면 빌드를 실패시킨다(예: 사전이 translation_info 를 채우기 시작한 경우).
스키마는 HANDOFF.md 9장 v4. 아래는 v4에 더한 것: entries.original_text(원어 조각을 이어 붙임), entries.sup_no_source,
pos_entries/patterns/senses 의 seq(순서), relations.seq, comm_patterns_text 표.
"""
import argparse
import os
import re
import shutil
import sqlite3
import sys
import time
import zlib
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import stdict_fetch as sf
from stdict_stem import stem_variants

SCHEMA_VERSION = "v4.2"        # v4 + pronunciations.pron_plain(v4.1) + stem_alts(v4.2)
CACHE_DIR = Path.home() / ".cache" / "stdict-db"
OUT_PATH = sf.DB_PATH.parent / "stdict.sqlite"
PREV_PATH = sf.DB_PATH.parent / "stdict.prev.sqlite"
LINK_BAD_ERROR_RATE = 0.005          # 링크 검사에서 이 비율을 넘게 어긋나면 실패, 이하면 경고

# (코드, 표제어, 발음, 원어): 코드 재사용과 발음 오류를 함께 잡기 위해 넷을 같이 확인한다.
GOLDEN = [
    (236214, "연기", "연ː기", "演技"),
    (236208, "연기", "연ː기", "捐棄"),
    (459493, "연기", "연기", "延期"),
    (236213, "연기", "연기", "煙氣"),
    (358706, "풀-발", "풀빨", None),
]

# ---------------------------------------------------------------- 알고 있는 태그 (이 밖은 실패)
_LEX = {"word": None, "unit": None, "type": None, "link_target_code": None, "link": None}
_REL = {"word": None, "type": None, "link_target_code": None, "link": None}
_SENSE = {
    "sense_code": None, "type": None, "definition": None, "definition_original": None, "scientific_name": None,
    "sense_pattern_info": {"pattern": None}, "sense_grammar_info": {"grammar": None},
    "cat_info": {"cat": None},
    "example_info": {"example": None, "source": None, "origin": None, "translation": None},
    "multimedia_info": {"label": None, "lable": None, "type": None, "link": None},
    "lexical_info": _LEX,
}
TREE = {
    "target_code": None,
    "word_info": {
        "word": None, "word_unit": None, "word_type": None,
        "original_language_info": {"original_language": None, "language_type": None},
        "pronunciation_info": {"pronunciation": None},
        "conju_info": {
            "conjugation_info": {"conjugation": None, "pronunciation_info": {"pronunciation": None}},
            "abbreviation_info": {"abbreviation": None, "pronunciation_info": {"pronunciation": None}},
        },
        "relation_info": _REL, "origin": None, "allomorph": None, "lexical_info": _LEX,
        "norm_info": {"type": None, "desc": None},
        "pos_info": {
            "pos_code": None, "pos": None, "lexical_info": _LEX,
            "comm_pattern_info": {
                "comm_pattern_code": None, "pattern_info": {"pattern": None}, "grammar_info": {"grammar": None},
                "lexical_info": _LEX, "sense_info": _SENSE,
            },
        },
    },
}

# ---------------------------------------------------------------- 스키마 v4
TABLES = {  # 표 이름 -> 열 정의(삽입 순서)
    "entries": "target_code INTEGER PRIMARY KEY, word TEXT NOT NULL, word_plain TEXT NOT NULL, word_key TEXT NOT NULL, sup_no INTEGER, "
               "sup_no_source TEXT, word_unit TEXT, word_type TEXT, original_text TEXT, origin TEXT, allomorph TEXT",
    "origins": "target_code INTEGER NOT NULL, seq INTEGER NOT NULL, original_language TEXT, language_type TEXT",
    "pronunciations": "target_code INTEGER NOT NULL, seq INTEGER NOT NULL, pronunciation TEXT, pron_norm TEXT, pron_plain TEXT",
    "conjugations": "id INTEGER PRIMARY KEY, target_code INTEGER NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, form TEXT",
    "conj_pron": "conjugation_id INTEGER NOT NULL, seq INTEGER NOT NULL, pronunciation TEXT",
    "pos_entries": "pos_code INTEGER PRIMARY KEY, target_code INTEGER NOT NULL, seq INTEGER NOT NULL, pos TEXT",
    "patterns": "comm_pattern_code INTEGER PRIMARY KEY, pos_code INTEGER NOT NULL, seq INTEGER NOT NULL",
    "comm_patterns_text": "comm_pattern_code INTEGER NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT",
    "senses": "sense_code INTEGER PRIMARY KEY, comm_pattern_code INTEGER NOT NULL, seq INTEGER NOT NULL, type TEXT, "
              "definition TEXT, definition_original TEXT, scientific_name TEXT",
    "sense_cats": "sense_code INTEGER NOT NULL, cat TEXT",
    "sense_patterns": "sense_code INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT",
    "examples": "sense_code INTEGER NOT NULL, seq INTEGER NOT NULL, example TEXT, source TEXT, origin TEXT, translation TEXT",
    "multimedia": "sense_code INTEGER NOT NULL, seq INTEGER NOT NULL, type TEXT, label TEXT, link TEXT",
    "norm_info": "target_code INTEGER NOT NULL, seq INTEGER NOT NULL, type TEXT, desc TEXT",
    "relations": "scope TEXT NOT NULL, scope_code INTEGER NOT NULL, seq INTEGER NOT NULL, unit TEXT, rel_type TEXT, "
                 "word TEXT, link_code INTEGER, link_entry_code INTEGER",
    "meta": "key TEXT PRIMARY KEY, value TEXT",
}
NCOLS = {t: len(c.split(",")) for t, c in TABLES.items()}      # 열 정의에는 쉼표가 열 구분으로만 쓰인다

INDEXES = """
CREATE INDEX entries_word_plain ON entries(word_plain);
CREATE INDEX entries_word_key ON entries(word_key);
CREATE INDEX entries_word ON entries(word);
CREATE INDEX origins_code ON origins(target_code);
CREATE INDEX origins_lang ON origins(original_language);
CREATE INDEX pron_code ON pronunciations(target_code);
CREATE INDEX pron_norm ON pronunciations(pron_norm);
CREATE INDEX pron_plain ON pronunciations(pron_plain);
CREATE INDEX conj_code ON conjugations(target_code);
CREATE INDEX conj_form ON conjugations(form);
CREATE INDEX conj_pron_id ON conj_pron(conjugation_id);
CREATE INDEX pos_code_entry ON pos_entries(target_code);
CREATE INDEX patterns_pos ON patterns(pos_code);
CREATE INDEX comm_text_code ON comm_patterns_text(comm_pattern_code);
CREATE INDEX senses_pattern ON senses(comm_pattern_code);
CREATE INDEX sense_cats_code ON sense_cats(sense_code);
CREATE INDEX sense_cats_cat ON sense_cats(cat);
CREATE INDEX sense_patterns_code ON sense_patterns(sense_code);
CREATE INDEX examples_code ON examples(sense_code);
CREATE INDEX multimedia_code ON multimedia(sense_code);
CREATE INDEX norm_code ON norm_info(target_code);
CREATE INDEX relations_scope ON relations(scope, scope_code);
CREATE INDEX relations_link_entry ON relations(link_entry_code);
CREATE INDEX relations_link_code ON relations(link_code);
"""


class BuildError(Exception):
    pass


def norm(s):
    if s is None:
        return None
    s = " ".join(s.split())
    return s or None


def tx(e, path):
    n = e.find(path)
    return norm(n.text) if n is not None else None


def to_int(s):
    return int(s) if s and s.isdigit() else None


def plain_key(word):
    """하이픈(-)과 ^ 를 뺀 표제어(word_plain). 띄어쓰기는 남긴다."""
    return re.sub(r"[-^]", "", word)


def word_key(word):
    """하이픈·^·띄어쓰기를 모두 뺀 키(word_key). 어깨번호는 이 기준으로 매겨지고(stdict_sync.py supno 와 같은 규칙),
    사전 검색도 '감정교육'/'감정 교육'을 같은 표제어로 묶는다."""
    return re.sub(r"[-^\s]", "", word)


# ---------------------------------------------------------------- 파싱
def find_unknown(elem, tree, path, out):
    for child in elem:
        sub = tree.get(child.tag, False)
        p = f"{path}/{child.tag}"
        if sub is False:
            out[p] += 1
        elif sub is None:
            if len(child):
                out[p + "(안에 태그가 있음)"] += 1
        else:
            find_unknown(child, sub, p, out)


def add_rel(R, scope, scope_code, seq, e, with_unit):
    link = norm(e.findtext("link"))
    entry = None
    if link:
        q = parse_qs(urlparse(link).query).get("word_no")
        entry = to_int(q[0]) if q else None
    R["relations"].append((scope, scope_code, seq, tx(e, "unit") if with_unit else None, tx(e, "type"),
                           tx(e, "word"), to_int(tx(e, "link_target_code")), entry))


def parse_item(item, R, ids):
    code = int(item.findtext("target_code").strip())
    wi = item.find("word_info")
    if wi is None:
        raise BuildError(f"{code}: word_info 없음")
    word = tx(wi, "word")
    if not word:
        raise BuildError(f"{code}: 표제어 없음")
    origins = wi.findall("original_language_info")
    R["entries"].append((code, word, plain_key(word), word_key(word), None, None, tx(wi, "word_unit"), tx(wi, "word_type"),
                         "".join(tx(o, "original_language") or "" for o in origins) or None,
                         tx(wi, "origin"), tx(wi, "allomorph")))
    for i, o in enumerate(origins):
        R["origins"].append((code, i, tx(o, "original_language"), tx(o, "language_type")))
    for i, p in enumerate(wi.findall("pronunciation_info")):
        pr = tx(p, "pronunciation")
        pn = pr.replace("ː", ":") if pr else None
        R["pronunciations"].append((code, i, pr, pn, pn.replace(":", "") if pn else None))
    seq = 0
    for cj in wi.findall("conju_info"):
        for kind, tag, ctag in (("활용", "conjugation_info", "conjugation"), ("준말", "abbreviation_info", "abbreviation")):
            for ci in cj.findall(tag):
                ids["conj"] += 1
                R["conjugations"].append((ids["conj"], code, seq, kind, tx(ci, ctag)))
                for j, p in enumerate(ci.findall("pronunciation_info")):
                    R["conj_pron"].append((ids["conj"], j, tx(p, "pronunciation")))
                seq += 1
    for i, r in enumerate(wi.findall("relation_info")):
        add_rel(R, "relation_info", code, i, r, False)
    for i, l in enumerate(wi.findall("lexical_info")):
        add_rel(R, "word", code, i, l, True)
    for i, n in enumerate(wi.findall("norm_info")):
        R["norm_info"].append((code, i, tx(n, "type"), tx(n, "desc")))
    for pi, pos in enumerate(wi.findall("pos_info")):
        pos_code = int(pos.findtext("pos_code").strip())
        R["pos_entries"].append((pos_code, code, pi, tx(pos, "pos")))
        for i, l in enumerate(pos.findall("lexical_info")):
            add_rel(R, "pos", pos_code, i, l, True)
        for ci, cp in enumerate(pos.findall("comm_pattern_info")):
            cpc = int(cp.findtext("comm_pattern_code").strip())
            R["patterns"].append((cpc, pos_code, ci))
            k = 0
            for kind, box, leaf in (("pattern", "pattern_info", "pattern"), ("grammar", "grammar_info", "grammar")):
                for b in cp.findall(box):
                    for x in b.findall(leaf):
                        R["comm_patterns_text"].append((cpc, k, kind, norm(x.text)))
                        k += 1
            for i, l in enumerate(cp.findall("lexical_info")):
                add_rel(R, "pattern", cpc, i, l, True)
            for si, s in enumerate(cp.findall("sense_info")):
                sc = int(s.findtext("sense_code").strip())
                R["senses"].append((sc, cpc, si, tx(s, "type"), tx(s, "definition"), tx(s, "definition_original"),
                                    tx(s, "scientific_name")))
                for c in s.findall("cat_info/cat"):
                    R["sense_cats"].append((sc, norm(c.text)))
                for kind, path in (("pattern", "sense_pattern_info/pattern"), ("grammar", "sense_grammar_info/grammar")):
                    for x in s.findall(path):
                        R["sense_patterns"].append((sc, kind, norm(x.text)))
                for ei, ex in enumerate(s.findall("example_info")):
                    R["examples"].append((sc, ei, tx(ex, "example"), tx(ex, "source"), tx(ex, "origin"),
                                          tx(ex, "translation")))
                for mi, m in enumerate(s.findall("multimedia_info")):
                    R["multimedia"].append((sc, mi, tx(m, "type"), tx(m, "label") or tx(m, "lable"), tx(m, "link")))
                for i, l in enumerate(s.findall("lexical_info")):
                    add_rel(R, "sense", sc, i, l, True)


def flush(db, R):
    for t, rows in R.items():
        if rows:
            try:
                db.executemany(f"INSERT INTO {t} VALUES({','.join('?' * NCOLS[t])})", rows)
            except sqlite3.IntegrityError as e:
                raise BuildError(f"{t} 표에 중복 키가 있습니다: {e} (예: {rows[0][:3]}...). 원본 코드가 겹치는 경우이므로 알려 주세요.")
            rows.clear()


def load(raw, db, limit):
    q = ("SELECT r.target_code, r.xml FROM raw_xml r JOIN fetch_state f ON f.target_code=r.target_code "
         "WHERE f.status='ok' ORDER BY r.target_code")
    if limit:
        q += f" LIMIT {int(limit)}"
    R = {t: [] for t in TABLES if t != "meta"}
    ids, tally, unknown, bad = {"conj": 0}, Counter(), Counter(), []
    n = 0
    t0 = time.time()
    for code, blob in raw.execute(q):
        one = {t: [] for t in R}                            # 항목 하나가 깨져도 반쯤 넣은 행이 남지 않게 따로 모은다
        try:
            root = ET.fromstring(zlib.decompress(blob).decode("utf-8").strip())
            item = root.find("item") if root.tag != "item" else root
            if item is None:
                raise BuildError(f"{code}: item 없음")
            parse_item(item, one, ids)
        except Exception as e:                              # 끝까지 돌려 문제를 모두 모으고, 하나라도 있으면 빌드는 실패
            bad.append(f"{code}: {type(e).__name__} {e}")
            continue
        for t, rows in one.items():
            R[t].extend(rows)
        find_unknown(item, TREE, "item", unknown)
        tally.update(e.tag for e in item.iter())
        n += 1
        if n % 5000 == 0:
            flush(db, R)
        if n % 50000 == 0:
            print(f"  {n:,}건 처리 ({time.time() - t0:.0f}초)")
    flush(db, R)
    db.commit()
    return n, tally, unknown, bad


# ---------------------------------------------------------------- 후처리: 어깨번호, 인덱스, FTS
def assign_sup_no(db, raw):
    """우선순위: 검색 결과(search_hits) > 어휘 링크의 번호('계년02') > 단독 표제어 0. 나머지는 NULL(supno 보충 필요)."""
    hits = {int(c): int(s) for c, s in raw.execute(
        "SELECT target_code, sup_no FROM search_hits WHERE sup_no GLOB '[0-9]*'")}
    have = {r[0] for r in db.execute("SELECT target_code FROM entries")}
    db.executemany("UPDATE entries SET sup_no=?, sup_no_source='search' WHERE target_code=?",
                   [(s, c) for c, s in hits.items() if c in have])
    db.execute(
        "CREATE TEMP TABLE link_sup AS SELECT r.link_code AS code, MIN(CAST(substr(r.word,-2) AS INTEGER)) AS n "
        "FROM relations r JOIN entries e ON e.target_code=r.link_code "
        "WHERE (r.scope='relation_info' OR r.unit='어휘') AND substr(r.word,-2) GLOB '[0-9][0-9]' "
        "AND substr(r.word,1,length(r.word)-2)=e.word GROUP BY r.link_code")
    db.execute("UPDATE entries SET sup_no=(SELECT n FROM link_sup WHERE code=entries.target_code), sup_no_source='link' "
               "WHERE sup_no IS NULL AND target_code IN (SELECT code FROM link_sup)")
    db.execute("DROP TABLE link_sup")
    db.execute("UPDATE entries SET sup_no=0, sup_no_source='single' WHERE sup_no IS NULL AND word_key IN "
               "(SELECT word_key FROM entries GROUP BY word_key HAVING COUNT(*)=1)")
    db.commit()


def finish(db, args, raw):
    print("인덱스 생성...")
    db.executescript(INDEXES)
    print("어깨번호 배정...")
    assign_sup_no(db, raw)
    print("FTS5 색인 생성...")
    db.executescript("""
        CREATE VIRTUAL TABLE fts_words USING fts5(word_plain, original_text, pron, conj, tokenize='trigram');
        CREATE VIRTUAL TABLE fts_defs USING fts5(definition, tokenize='trigram');
    """)
    db.execute(
        "INSERT INTO fts_words(rowid, word_plain, original_text, pron, conj) "
        "SELECT e.target_code, e.word_plain, COALESCE(e.original_text,''), "
        " COALESCE((SELECT group_concat(p.pron_norm || ' ' || replace(p.pron_norm, ':', ''), ' ') FROM pronunciations p "
        "           WHERE p.target_code=e.target_code),''), "
        " COALESCE((SELECT group_concat(c.form, ' ') FROM conjugations c WHERE c.target_code=e.target_code),'') "
        "FROM entries e")
    db.execute("INSERT INTO fts_defs(rowid, definition) SELECT sense_code, definition FROM senses WHERE definition IS NOT NULL")
    db.commit()
    build_stem_alts(db)


def build_stem_alts(db):
    """동사·형용사가 사전에 싣는 활용형에서 불규칙 어간 변이형을 뽑아 stem_alts(stem, target_code, source_form)에 저장한다.
    조회가 '들었다'에서 듣다(ㄷ 불규칙)를 근거와 함께 찾는 데 쓴다."""
    print("불규칙 어간 변이형 추출...")
    db.execute("CREATE TABLE stem_alts(stem TEXT NOT NULL, target_code INTEGER NOT NULL, source_form TEXT NOT NULL)")
    forms = {}
    for code, base, form in db.execute(
            "SELECT e.target_code, e.word_key, c.form FROM entries e JOIN conjugations c USING(target_code) "
            "WHERE e.word_key LIKE '%다' AND c.form IS NOT NULL AND EXISTS (SELECT 1 FROM pos_entries p "
            "WHERE p.target_code=e.target_code AND p.pos IN ('동사','형용사','보조 동사','보조 형용사')) ORDER BY c.id"):
        forms.setdefault((code, base), []).append(form)
    rows = [(v, code, src) for (code, base), fs in forms.items() for v, src in stem_variants(base, fs).items()]
    db.executemany("INSERT INTO stem_alts VALUES(?,?,?)", rows)
    db.execute("CREATE INDEX stem_alts_stem ON stem_alts(stem)")
    db.commit()
    print(f"  {len(rows):,}개 ({len({r[1] for r in rows}):,}개 표제어)")


# ---------------------------------------------------------------- 검증
def scalar(db, sql, *a):
    return db.execute(sql, a).fetchone()[0]


def validate(db, args, n_ok, tally, unknown, bad, partial_keys=frozenset()):
    errors, warns = [], []
    # 1) 파싱 실패, 모르는 태그
    if bad:
        errors.append(f"파싱 실패 {len(bad):,}건 (예: {'; '.join(bad[:3])})")
    if unknown and not args.allow_unknown:
        errors.append("TREE 에 없는 태그: " + ", ".join(f"{p} {c:,}건" for p, c in unknown.most_common(8))
                      + " → 스키마와 파서에 반영한 뒤 빌드하세요(--allow-unknown 으로 무시 가능, 권장하지 않음)")
    # 2) 건수 대조
    count = lambda t: scalar(db, f"SELECT COUNT(*) FROM {t}")
    if count("entries") != n_ok:
        errors.append(f"entries {count('entries'):,} != 원본 ok {n_ok:,}")
    pairs = [("senses", tally["sense_info"]), ("examples", tally["example_info"]), ("pos_entries", tally["pos_info"]),
             ("patterns", tally["comm_pattern_info"]), ("relations", tally["lexical_info"] + tally["relation_info"]),
             ("norm_info", tally["norm_info"]), ("multimedia", tally["multimedia_info"]),
             ("origins", tally["original_language_info"]), ("sense_cats", tally["cat"]),
             ("conjugations", tally["conjugation_info"] + tally["abbreviation_info"])]
    for t, want in pairs:
        if count(t) != want:
            errors.append(f"{t} {count(t):,} != 원본 태그 수 {want:,}")
    if count("pronunciations") + count("conj_pron") != tally["pronunciation_info"]:
        errors.append("발음 행 수가 원본 pronunciation_info 수와 다름")
    if count("comm_patterns_text") + count("sense_patterns") != tally["pattern"] + tally["grammar"]:
        errors.append("문형·문법 행 수가 원본 pattern+grammar 수와 다름")
    # 3) 구조
    if scalar(db, "SELECT COUNT(*) FROM entries e WHERE NOT EXISTS (SELECT 1 FROM pos_entries p WHERE p.target_code=e.target_code)"):
        errors.append("품사가 없는 항목이 있음")
    nosense = scalar(db, "SELECT COUNT(*) FROM entries e WHERE NOT EXISTS (SELECT 1 FROM pos_entries p "
                         "JOIN patterns c ON c.pos_code=p.pos_code JOIN senses s ON s.comm_pattern_code=c.comm_pattern_code "
                         "WHERE p.target_code=e.target_code)")
    if nosense:
        warns.append(f"뜻풀이(sense_info)가 없는 항목 {nosense:,}건(원본에도 없음, 2026-10-12 기준 3건)")
    # 4) 골든 테스트: (코드, 표제어, 발음, 원어)
    if not args.limit:
        for code, word, pron, orig in GOLDEN:
            row = db.execute("SELECT word, original_text FROM entries WHERE target_code=?", (code,)).fetchone()
            prons = [r[0] for r in db.execute("SELECT pronunciation FROM pronunciations WHERE target_code=?", (code,))]
            if row is None or row[0] != word or pron not in prons or (orig and row[1] != orig):
                errors.append(f"골든 테스트 실패 {code}: 기대 ({word}, {pron}, {orig}), 실제 ({row}, {prons})")
    # 5) 어깨번호
    # 번호 0 은 '번호 없는 표기'(검색 결과가 0 으로 돌려준 것)라서 허용한다. 0 이 아닌 번호는 서로 달라야 하고 그룹 크기를 넘지 않아야 한다.
    groups = db.execute("SELECT word_key, COUNT(*), COUNT(sup_no), COUNT(NULLIF(sup_no,0)), MIN(NULLIF(sup_no,0)), "
                        "MAX(sup_no), COUNT(DISTINCT NULLIF(sup_no,0)) FROM entries GROUP BY word_key HAVING COUNT(*)>1").fetchall()
    invalid = [g for g in groups if g[3] and (g[5] > g[1] or g[6] != g[3])]
    incomplete = [g for g in groups if g[2] < g[1]]
    known_gaps = [g for g in incomplete if g[0] in partial_keys]      # supno 가 '검색 색인에 없는 로컬 항목'으로 기록한 그룹
    unexplained = [g for g in incomplete if g[0] not in partial_keys]
    if invalid:   # 시험 빌드(--limit)는 그룹 일부만 있으므로 번호가 그룹 크기를 넘는 것이 정상
        (warns if args.limit else errors).append(
            f"어깨번호가 1..n 규칙에 어긋난 중복 그룹 {len(invalid):,}개 (예: {[g[0] for g in invalid[:3]]})")
    if known_gaps:
        warns.append(f"검색 색인에 없는 로컬 항목 때문에 어깨번호를 알 수 없는 중복 그룹 {len(known_gaps):,}개"
                     f"({sum(g[1] - g[2] for g in known_gaps):,}건, supno_issues 에 'partial'로 기록된 알려진 예외, 번호 없이 표시됨)")
    if unexplained:
        msg = (f"어깨번호가 비어 있는 중복 그룹 {len(unexplained):,}개(알려진 예외 아님) → `stdict_sync.py supno` 로 보충 필요 "
               f"(sup_no NULL {scalar(db, 'SELECT COUNT(*) FROM entries WHERE sup_no IS NULL'):,}건)")
        (errors if args.require_supno else warns).append(msg)
    conflict = scalar(db, "SELECT COUNT(*) FROM relations r JOIN entries e ON e.target_code=r.link_code "
                          "WHERE (r.scope='relation_info' OR r.unit='어휘') AND e.sup_no IS NOT NULL "
                          "AND substr(r.word,-2) GLOB '[0-9][0-9]' AND substr(r.word,1,length(r.word)-2)=e.word "
                          "AND CAST(substr(r.word,-2) AS INTEGER)<>e.sup_no")
    if conflict:
        errors.append(f"링크의 어깨번호와 배정한 번호가 다른 것 {conflict:,}건")
    # 6) 링크 정합성
    if not args.limit:
        link_checks(db, errors, warns)
    return errors, warns


def link_checks(db, errors, warns):
    """링크가 가리키는 대상은 스코프가 아니라 unit 으로 정해진다: 어휘/관련 어휘 -> 표제어 코드,
    의미 -> sense_code, 공통 문형 -> comm_pattern_code, 품사 -> pos_code. 대상이 있는지, 표제어가 맞는지 본다."""
    def rate(name, total, bad):
        if not total:
            return
        r = bad / total
        line = f"링크 {name}: {bad:,}/{total:,} ({r * 100:.3f}%)"
        if bad and r > LINK_BAD_ERROR_RATE and bad > 5:      # 표본이 작으면 몇 건 어긋나도 비율이 커 보이므로 건수도 함께 본다
            errors.append(line)
        elif bad:
            warns.append(line)
        else:
            print(f"  {line}")
    base_eq = "(r.word=e.word OR (substr(r.word,-2) GLOB '[0-9][0-9]' AND substr(r.word,1,length(r.word)-2)=e.word))"
    is_entry = "(r.scope='relation_info' OR r.unit='어휘')"
    known_units = ("어휘", "의미", "공통 문형", "품사")
    other = scalar(db, "SELECT COUNT(*) FROM relations r WHERE r.scope<>'relation_info' AND "
                       f"(r.unit IS NULL OR r.unit NOT IN {known_units})")
    if other:
        errors.append(f"알 수 없는 링크 unit {other:,}건 (예: " + ", ".join(
            str(r[0]) for r in db.execute("SELECT DISTINCT unit FROM relations r WHERE r.scope<>'relation_info' AND "
                                          f"(r.unit IS NULL OR r.unit NOT IN {known_units}) LIMIT 5")) + ")")
    # 표제어(어휘, 관련 어휘)
    tot = scalar(db, f"SELECT COUNT(*) FROM relations r WHERE {is_entry} AND r.link_code IS NOT NULL")
    dangling = scalar(db, f"SELECT COUNT(*) FROM relations r WHERE {is_entry} AND r.link_code IS NOT NULL "
                          "AND NOT EXISTS (SELECT 1 FROM entries e WHERE e.target_code=r.link_code)")
    rate("어휘·관련 어휘 대상 코드 없음", tot, dangling)
    rate("어휘·관련 어휘 표제어 불일치", tot - dangling, scalar(
        db, "SELECT COUNT(*) FROM relations r JOIN entries e ON e.target_code=r.link_code "
            f"WHERE {is_entry} AND NOT {base_eq}"))
    # 의미
    tot = scalar(db, "SELECT COUNT(*) FROM relations r WHERE r.unit='의미' AND r.link_code IS NOT NULL")
    dangling = scalar(db, "SELECT COUNT(*) FROM relations r WHERE r.unit='의미' AND r.link_code IS NOT NULL "
                          "AND NOT EXISTS (SELECT 1 FROM senses s WHERE s.sense_code=r.link_code)")
    rate("의미 대상 코드(sense_code) 없음", tot, dangling)
    j = ("FROM relations r JOIN senses s ON s.sense_code=r.link_code JOIN patterns p ON p.comm_pattern_code=s.comm_pattern_code "
         "JOIN pos_entries pe ON pe.pos_code=p.pos_code JOIN entries e ON e.target_code=pe.target_code WHERE r.unit='의미'")
    rate("의미 링크의 표제어 코드(word_no) 불일치", tot - dangling,
         scalar(db, f"SELECT COUNT(*) {j} AND r.link_entry_code IS NOT NULL AND pe.target_code<>r.link_entry_code"))
    rate("의미 링크 표제어 불일치", tot - dangling, scalar(db, f"SELECT COUNT(*) {j} AND NOT {base_eq}"))
    # 공통 문형, 품사
    for unit, tbl, col, extra in (("공통 문형", "patterns", "comm_pattern_code", "JOIN pos_entries pe ON pe.pos_code=x.pos_code"),
                                   ("품사", "pos_entries", "pos_code", "JOIN pos_entries pe ON pe.pos_code=x.pos_code")):
        tot = scalar(db, "SELECT COUNT(*) FROM relations r WHERE r.unit=? AND r.link_code IS NOT NULL", unit)
        if not tot:
            continue
        dangling = scalar(db, f"SELECT COUNT(*) FROM relations r WHERE r.unit=? AND r.link_code IS NOT NULL "
                              f"AND NOT EXISTS (SELECT 1 FROM {tbl} x WHERE x.{col}=r.link_code)", unit)
        rate(f"{unit} 대상 코드 없음", tot, dangling)
        bad = scalar(db, f"SELECT COUNT(*) FROM relations r JOIN {tbl} x ON x.{col}=r.link_code {extra} "
                         "JOIN entries e ON e.target_code=pe.target_code "
                         f"WHERE r.unit=? AND NOT {base_eq}", unit)
        rate(f"{unit} 링크 표제어 불일치", tot - dangling, bad)


# ---------------------------------------------------------------- 교체
def swap(new, out, prev):
    out, prev = Path(out), Path(prev)
    new = Path(new)
    if os.stat(new).st_dev != os.stat(out.parent).st_dev:         # 다른 볼륨이면 같은 폴더로 복사해서 이름 바꾸기
        staged = out.with_name(out.name + ".new")
        shutil.copyfile(new, staged)
        new.unlink()
        new = staged
    had = out.exists()
    if had:
        os.replace(out, prev)
    try:
        os.replace(new, out)
    except Exception:
        if had and not out.exists():
            os.replace(prev, out)
        raise


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(OUT_PATH), help="완성본 위치(기본 data/stdict.sqlite)")
    ap.add_argument("--prev", default=str(PREV_PATH), help="교체 전 버전 보관 위치(기본 data/stdict.prev.sqlite, 1개만 보관)")
    ap.add_argument("--tmp-dir", default=str(CACHE_DIR), help="임시 빌드 폴더(rsync 되는 폴더 밖, 기본 ~/.cache/stdict-db)")
    ap.add_argument("--limit", type=int, help="앞의 N건만 빌드(시험용, 교체하지 않음)")
    ap.add_argument("--require-supno", action="store_true", help="어깨번호가 빈 중복 그룹이 있으면 실패로 처리")
    ap.add_argument("--allow-unknown", action="store_true", help="TREE 에 없는 태그가 있어도 실패시키지 않음(비권장)")
    ap.add_argument("--keep-failed", action="store_true", help="검증에 실패해도 임시 DB를 남김(원인 조사용)")
    args = ap.parse_args()

    if not sf.DB_PATH.exists():
        sys.exit("원본 DB(data/raw.sqlite)가 없습니다.")
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
    except sqlite3.Error as e:
        sys.exit(f"이 파이썬의 SQLite 가 FTS5 trigram 을 지원하지 않습니다: {e}")
    tmp_dir = Path(args.tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / ("stdict.test.sqlite" if args.limit else "stdict.building.sqlite")
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(tmp) + suffix).unlink(missing_ok=True)

    raw = sqlite3.connect(f"file:{sf.DB_PATH}?mode=ro", uri=True)
    n_total = raw.execute("SELECT COUNT(*) FROM raw_xml r JOIN fetch_state f ON f.target_code=r.target_code "
                          "WHERE f.status='ok'").fetchone()[0]
    print(f"원본 ok {n_total:,}건" + (f" 중 앞의 {args.limit:,}건만 빌드(시험)" if args.limit else "") + f" → {tmp}")
    db = sqlite3.connect(tmp)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA cache_size=-400000")
    for t, cols in TABLES.items():
        db.execute(f"CREATE TABLE {t}({cols})")
    t0 = time.time()
    try:
        print("원본 파싱...")
        n_ok, tally, unknown, bad = load(raw, db, args.limit)
        finish(db, args, raw)
        meta = {"schema_version": SCHEMA_VERSION, "built_at": sf.now(), "source_ok_count": str(n_total),
                "fts_tokenizer": "trigram (3글자 미만 검색은 인덱스 컬럼 word_plain/pron_norm/form 으로)",
                "source_last_updates_date": (raw.execute("SELECT value FROM sync_meta WHERE key='last_updates_date'")
                                             .fetchone() or [None])[0] or ""}
        db.executemany("INSERT INTO meta VALUES(?,?)", list(meta.items()))
        db.commit()
        print("검증...")
        try:
            partial_keys = {r[0] for r in raw.execute("SELECT plain FROM supno_issues WHERE kind='partial'")}
        except sqlite3.Error:
            partial_keys = set()
        errors, warns = validate(db, args, min(args.limit, n_total) if args.limit else n_total, tally, unknown, bad, partial_keys)
        if errors:
            raise BuildError("\n".join(errors))
        db.execute("ANALYZE")
        db.execute("PRAGMA journal_mode=DELETE")
        db.commit()
        ic = db.execute("PRAGMA integrity_check").fetchone()[0]
        if ic != "ok":
            raise BuildError(f"integrity_check: {ic}")
    except BuildError as e:
        db.close()
        print(f"\n빌드 실패 (조회 중인 DB는 그대로입니다):\n{e}")
        if args.keep_failed:
            print(f"임시 DB를 남겼습니다: {tmp}")
        else:
            for suffix in ("", "-journal"):
                Path(str(tmp) + suffix).unlink(missing_ok=True)
        sys.exit(1)
    counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES if t != "meta"}
    db.close()
    print(f"\n빌드 완료 ({time.time() - t0:.0f}초, {os.path.getsize(tmp) / 1e6:,.0f}MB)")
    print("  " + ", ".join(f"{t} {c:,}" for t, c in counts.items()))
    for w in warns:
        print(f"  ⚠ {w}")
    if args.limit:
        print(f"시험 빌드라 교체하지 않았습니다: {tmp}")
        return
    had_prev = Path(args.out).exists()
    swap(tmp, args.out, args.prev)
    print(f"교체 완료: {args.out}" + (f" (이전 버전: {args.prev})" if had_prev else " (첫 빌드라 이전 버전 없음)"))


if __name__ == "__main__":
    main()
