"""정정 인지형 근거 패키지 — filings.py의 정정체인 API를 조회만 해서 구조화한다.

새 계산·새 판단을 하지 않는다. `filings.chain()`/`filings.diff()`가 이미 갖고 있는
정정 전/후 값을 순서대로 옮겨 적을 뿐이다. 여러 차례 정정됐으면 "최종 확정값"을
억지로 고르지 않고 이력 전체를 순서대로 담는다 — 사용자가 다 봐야 한다.

## diff_key로 맞춘다, label 텍스트로 맞추지 않는다

`filings.diff()`가 반환하는 항목의 "label"은 XBRL 계정이면
`f"{label_norm} (제{fiscal_term}기 연결/별도)"`처럼 fiscal_term·scope가 붙은
합성 문자열이다. label_norm만으로 `startswith` 매칭을 하면 "매출원가"가
"매출원가율"에도 걸리는 것처럼 계정끼리 서로 오매칭될 수 있다.

`diff()`가 실제로 항목을 색인하는 키(`d["key"]`)는 XBRL이면
`f"{aclass_xbrl_code}|{label_norm}|{fiscal_term}|{scope}"`(filings._xbrl_fields),
구조화 공시면 field_key 그대로(filings.build)다. 이 키를 evidence 쪽에서도 똑같이
재구성해 **정확히 일치**하는 항목만 고른다 — pipeline.py의 `to_coordinate`/
`struct_coordinate`가 이미 가진 필드(aclass_xbrl_code·label_norm·fiscal_term·scope
또는 field_key)만으로 재구성 가능해서 새 데이터 소스가 필요 없다.
"""

from . import filings


def correction_info(rcept_no, diff_key):
    """이 rcept_no 이후 정정 체인에서, diff_key가 일치하는 항목들의 전/후 값 이력.

    체인 안에서 rcept_no보다 뒤에 있는 정정본들을 순서대로(직전 → 다음) diff해서
    diff_key가 바뀐 칸만 뽑는다. 정정이 여러 번이면 단계별로 전부 담는다(최종값만
    골라내지 않는다). 하나도 없으면 None — 정정 이력이 없다는 뜻이라 빈 리스트가
    아니라 None으로 fail-closed 한다.
    """
    if not rcept_no or not diff_key:
        return None
    fl = filings.get()
    chain = fl.chain(rcept_no)
    if rcept_no not in chain:
        return None
    idx = chain.index(rcept_no)
    later = chain[idx + 1:]
    if not later:
        return None

    history = []
    cur = rcept_no
    for nxt in later:
        for d in fl.diff(cur, nxt):
            if d.get("key") != diff_key:
                continue
            meta = fl.meta.get(nxt) or {}
            history.append({
                "rcept_no": nxt,
                "rcept_dt": meta.get("rcept_dt", ""),
                "report_nm": meta.get("report_nm", ""),
                "before": d.get("before"),
                "after": d.get("after"),
                "before_dec": d.get("before_dec"),
                "after_dec": d.get("after_dec"),
            })
        cur = nxt                          # 다음 구간은 이 정정본을 기준으로 이어서 비교한다

    return history or None
