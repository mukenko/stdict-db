"""규칙 활용형에서 어간 후보를 만든다(표준 라이브러리만, DB를 읽지 않음).

사전이 싣는 활용형은 항목당 몇 개뿐이라 '먹었다'·'갔다' 같은 활용형으로는 표제어를 못 찾는다. 여기서는 어미를 떼고
어간을 거꾸로 추정한다. 후보가 실제 표제어(동사·형용사)인지는 호출하는 쪽(stdict_lookup)이 사전에서 확인한다.

1단계 범위: **규칙 활용만**. ㅂ·ㄷ·ㅅ·ㅎ·르·러·으 불규칙, ㄹ 탈락(안다·아는·압니다)은 다루지 않는다.
그래서 불규칙 활용형은 못 찾거나, 모양이 같은 다른 표제어가 나올 수 있다(예: '들었다' → 들다, 실제는 듣다일 수도 있음).
"""
import unicodedata

_BASE, _N_V, _N_T = 0xAC00, 21, 28

# 모음 번호(ㅏ0 ㅐ1 ㅑ2 ㅒ3 ㅓ4 ㅔ5 ㅕ6 ㅖ7 ㅗ8 ㅘ9 ㅙ10 ㅚ11 ㅛ12 ㅜ13 ㅝ14 ㅞ15 ㅟ16 ㅠ17 ㅡ18 ㅢ19 ㅣ20)
# 받침 번호(ㄴ4 ㄹ8 ㅁ16 ㅂ17 ㅆ20), 초성 ㅎ=18
_A, _AE, _EO, _YEO, _E, _O, _WA, _WAE, _OE, _U, _WO, _I = 0, 1, 4, 6, 5, 8, 9, 10, 11, 13, 14, 20
_T_N, _T_L, _T_M, _T_B, _T_SS = 4, 8, 16, 17, 20
_L_H = 18


def _split(ch):
    n = ord(ch) - _BASE
    if not 0 <= n < 11172:
        return None
    return n // (_N_V * _N_T), (n % (_N_V * _N_T)) // _N_T, n % _N_T


def _join(l, v, t=0):
    return chr(_BASE + (l * _N_V + v) * _N_T + t)


# 어간을 그대로 두고 떼는 어미(모음으로 끝난 어간에도, 받침 있는 어간에도 붙는 것)
DIRECT = ("고", "지", "게", "며", "니", "네", "지만", "기", "자", "는", "는다", "는데", "거나", "도록", "겠다", "면", "서", "요", "도",
          "야", "라")
# 받침 있는 어간 뒤의 '으-' 어미
AFTER_CONSONANT = ("은", "을", "음", "으면", "으니", "으며", "으니까", "으려고", "으러")
# 과거 표지 뒤에 오는 어미
PAST_TAILS = ("다", "고", "지만", "는데", "어요", "어서", "어", "으니", "으니까", "으면", "을", "던", "지", "네")
# 아/어 계열 어미 뒤에 오는 말
AEO_TAILS = ("", "서", "요", "도", "야", "라")
PAST = ("았", "었", "였")
AEO = ("아", "어", "여")
# 줄어든 과거(받침 ㅆ)와 줄어든 아/어(받침 없음)의 모음 -> 어간 끝 모음 후보
_FUSE = {
    _A: (_A,), _EO: (_EO,), _YEO: (_I, _YEO), _AE: (_AE,), _WA: (_O,), _WO: (_U,), _WAE: (_OE,), _E: (_E,),
}
# 받침으로 붙는 어미: 받침 -> (뒤에 오는 말, 설명)
_TAIL_T = {
    _T_N: (("", "-ㄴ"), ("다", "-ㄴ다")),
    _T_L: (("", "-ㄹ"),),
    _T_M: (("", "-ㅁ"),),
    _T_B: (("니다", "-ㅂ니다"), ("니까", "-ㅂ니까")),
}


def _vowel_stems(ch):
    """받침 없는 음절 ch(줄어든 형태)에서 어간 끝 음절 후보를 만든다."""
    sp = _split(ch)
    if not sp or sp[2] != 0:
        return []
    l, v, _ = sp
    out = []
    if l == _L_H and v == _AE:                              # 해 <- 하+여
        out.append((_join(l, _A), "하+여→해"))
    for sv in _FUSE.get(v, ()):
        out.append((_join(l, sv), f"{_join(l, sv)}+{'아' if sv in (_A, _O) else '어'}→{ch}"))
    return out


def candidates(word):
    """word(NFC) -> [(어간, 설명)]. 어간에 '다'를 붙여 표제어를 확인하면 된다. 중복 어간은 먼저 나온 설명만 남긴다."""
    word = unicodedata.normalize("NFC", word)
    found = {}

    def add(stem, rule):
        if stem and stem not in found:
            found[stem] = rule

    n = len(word)
    # 1) 어간 + 어미(어간은 그대로)
    for suf in DIRECT:
        if n > len(suf) and word.endswith(suf):
            add(word[:-len(suf)], f"어간 '{word[:-len(suf)]}' + 어미 '-{suf}'")
    for suf in AFTER_CONSONANT:
        if n > len(suf) and word.endswith(suf):
            add(word[:-len(suf)], f"받침 있는 어간 '{word[:-len(suf)]}' + 어미 '-{suf}'")
    # 2) 어간 + 아/어/여 (+서·요 …)
    for tail in AEO_TAILS:
        for a in AEO:
            suf = a + tail
            if n > len(suf) and word.endswith(suf):
                add(word[:-len(suf)], f"어간 '{word[:-len(suf)]}' + 어미 '-{suf}'")
    # 3) 어간 + 과거 표지(았/었/였) + 어미
    for tail in PAST_TAILS:
        for p in PAST:
            suf = p + tail
            if n > len(suf) and word.endswith(suf):
                stem = word[:-len(suf)]
                add(stem, f"어간 '{stem}' + 과거 '-{p}-' + '-{tail}'")
    # 4) 줄어든 과거: 마지막 음절의 받침 ㅆ (갔다 = 가+았+다)
    for tail in ("",) + PAST_TAILS:
        if tail and not word.endswith(tail):
            continue
        body = word[:-len(tail)] if tail else word
        if not body:
            continue
        sp = _split(body[-1])
        if sp and sp[2] == _T_SS:
            fused = _join(sp[0], sp[1])
            for stem_last, how in _vowel_stems(fused):
                stem = body[:-1] + stem_last
                add(stem, f"어간 '{stem}' + 과거 '-었-/-았-'({how}, ㅆ 받침으로 줄어듦)" + (f" + '-{tail}'" if tail else ""))
    # 5) 줄어든 아/어 (봐, 와, 줘, 해, 마셔, 돼, 가서, 해요 …)
    for tail in AEO_TAILS:
        if tail and not word.endswith(tail):
            continue
        body = word[:-len(tail)] if tail else word
        if not body:
            continue
        for stem_last, how in _vowel_stems(body[-1]):
            stem = body[:-1] + stem_last
            add(stem, f"어간 '{stem}' + 어미 '-아/어{tail}'({how}로 줄어듦)")
    # 6) 받침으로 붙는 어미 (간, 갈, 감, 간다, 갑니다)
    for t, tails in _TAIL_T.items():
        for tail, label in tails:
            if tail and not word.endswith(tail):
                continue
            body = word[:-len(tail)] if tail else word
            if not body:
                continue
            sp = _split(body[-1])
            if sp and sp[2] == t:
                stem = body[:-1] + _join(sp[0], sp[1])
                add(stem, f"받침 없는 어간 '{stem}' + 어미 '{label}'")
    for stem in list(found):                                # 기본형 + 어미('예쁘다고')면 '다'를 뗀 것도 어간 후보
        if stem.endswith("다") and len(stem) > 1:
            add(stem[:-1], f"기본형 '{stem}' + 어미 '-{word[len(stem):]}'")
    return list(found.items())
