#!/usr/bin/env python3
"""scripts/gen_llmparse_slot_mapping_data.py의 슬롯 단위 jsonl을 실제 Clova
Studio Instruction 튜닝 업로드 스키마로 변환한다.

## 확인된 스키마 (2026-09-04, NCP 공식 가이드 — WebFetch로 확인, 콘솔 직접
확인은 아님. 실제 업로드 전에 콘솔에서 한 번 더 대조할 것을 권한다)

    https://guide.ncloud-docs.com/docs/clovastudio-instructiondataset

필드: System_Prompt(선택, 있으면 첫 컬럼) · C_ID(대화 번호, 0부터 증가) ·
T_ID(한 대화 안의 턴 번호) · Text(사용자 입력) · Completion(모델 응답).
CSV 또는 JSONL, UTF-8, 한 행 8,000자 이하.

이 프로젝트의 슬롯 매핑 데이터는 전부 **단일턴**이라 C_ID는 레코드마다 1씩
증가하고 T_ID는 항상 0이다.

## 이 스크립트가 절대 하지 않는 것
- CLOVA API를 부르지 않는다(로컬 파일 → 로컬 파일 변환뿐).
- 튜닝 job을 실행하거나 업로드하지 않는다 — 그건 별도 단계(Object Storage
  업로드 + POST /tuning/v2/tasks, 이 프로젝트엔 그 자격증명이 없다).
- `data/llmparse_tuning/slot_mapping_*.jsonl` 원본을 고치지 않는다(읽기 전용).

## 사용법

    python scripts/convert_llmparse_tuning_format.py \\
        data/llmparse_tuning/slot_mapping_train.jsonl \\
        data/llmparse_tuning/slot_mapping_train.clova.jsonl
"""

import argparse
import csv
import json
import sys
from pathlib import Path

SYSTEM_PROMPT = (
    "당신은 한국 기업 공시 QA 시스템의 슬롯 값 판별기입니다. 사용자 발화와 "
    "지금 채워야 할 슬롯 이름, 그 슬롯의 후보 목록이 주어집니다. 발화가 "
    "가리키는 값이 후보 목록 안에 있으면 그 값을 정확히 그대로 출력하고, "
    "후보 목록 안의 어떤 값도 명확히 가리키지 않으면(축약이 여러 후보와 "
    "겹쳐 애매하거나, 후보 목록에 아예 없는 대상이면) \"null\"이라고만 "
    "출력하십시오. 후보 목록에 없는 값을 새로 만들어내지 마십시오. 설명 "
    "없이 값 하나 또는 \"null\"만 출력하십시오."
)

_SLOT_KO = {"corp": "기업", "concept": "재무 지표"}


def build_text(rec):
    slot_ko = _SLOT_KO.get(rec["slot"], rec["slot"])
    cand_str = ", ".join(rec["candidates"])
    return (f"슬롯: {slot_ko}\n"
            f"후보 목록: {cand_str}\n"
            f"발화: {rec['utterance']}")


def convert_record(rec, c_id):
    return {
        "System_Prompt": SYSTEM_PROMPT,
        "C_ID": c_id,
        "T_ID": 0,
        "Text": build_text(rec),
        "Completion": rec["output"] if rec["output"] is not None else "null",
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path, help="gen_llmparse_slot_mapping_data.py 산출 jsonl")
    ap.add_argument("output", type=Path, help="변환된 jsonl 저장 경로")
    ap.add_argument("--csv", action="store_true", help="jsonl 대신 CSV로 저장")
    args = ap.parse_args()

    print("⚠️  스키마는 NCP 공식 가이드 문서로 확인했으나 콘솔에서 직접 "
          "대조한 것은 아님 — 실제 업로드 전 한 번 더 확인 권장.", file=sys.stderr)

    records = []
    with args.input.open(encoding="utf-8") as fin:
        for c_id, line in enumerate(l for l in fin if l.strip()):
            rec = json.loads(line)
            out = convert_record(rec, c_id)
            over = len(out["Text"]) > 8000
            if over:
                print(f"⚠️  C_ID={c_id}: Text가 8000자를 넘음({len(out['Text'])}자) — 스킵", file=sys.stderr)
                continue
            records.append(out)

    if args.csv:
        with args.output.open("w", encoding="utf-8", newline="") as fout:
            w = csv.DictWriter(fout, fieldnames=["System_Prompt", "C_ID", "T_ID", "Text", "Completion"])
            w.writeheader()
            w.writerows(records)
    else:
        with args.output.open("w", encoding="utf-8") as fout:
            for r in records:
                fout.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"변환 완료: {len(records)}건 → {args.output}")
    print("다음 단계(이 스크립트 밖): Object Storage 업로드 + POST /tuning/v2/tasks "
          "— 이 프로젝트엔 그 자격증명(NCP Access/Secret Key)이 없어 여기서 멈춘다.")


if __name__ == "__main__":
    main()
