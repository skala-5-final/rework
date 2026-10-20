CALC_SYSTEM = """당신은 점포 운영 보고서의 작성 규칙을 원천 데이터에서 되찾는 분석가입니다.
보고서의 각 지표 슬롯이 원천 CSV의 어떤 열을 어떻게 계산한 값인지 규칙(CalcRule)으로 제안하세요.

규칙 작성 원칙:
- op: sum(합계), count_distinct(고유값 수, 예: 영업일수=business_date 고유값), daily_mean(합계 ÷ 영업일수), ratio(column 합계 ÷ denominator_column 합계)
- 단위(unit)가 천원·원이면 금액 열(…_amount_krw), 개·건·일이면 수량·건수·날짜 열을 쓰세요. 천원이면 scale=1000.
- 퍼센트(%)이면 ratio + as_percent=true.
- 보고서 값의 소수 자릿수를 보고 decimals를 정하세요. 반올림 방식은 기본 half_up.
- '매출'이 할인 전인지 후인지 등은 각주·업무 지침·데이터 사전을 근거로 판단하세요.
- 이전 시도가 불일치했다면 차이(diff)를 보고 열이나 연산을 바꾸세요. 같은 규칙을 반복하지 마세요.
- 허용 열만 쓰세요: {allowed}
"""

CALC_USER = """[원천 데이터 열]
{columns}

[보고서 각주]
{notes}

[업무 지침·데이터 사전 검색 결과]
{docs}

[검증할 슬롯] (cells는 보고서에 적힌 값 예시)
{slots}

[이전 시도와 불일치 내역]
{history}

위 슬롯 각각에 대해 CalcRule을 하나씩 제안하세요."""

DECOMPOSE_SYSTEM = """보고서의 '다음 달 제안'을 검증 가능한 결정(claim) 단위로 나누세요.
- 한 제안에 결정이 여러 개면 나누세요 (예: '제육 도시락 행사 제외' + '금요일 도시락 입고 확대').
- 같은 결정을 같은 이유로 받은 품목들은 하나의 claim으로 묶으세요 (예: '초코바·감자칩 행사 유지'는 claim 하나). 제안당 claim은 최대 4개.
- targets/comparators의 values는 반드시 [사용 가능한 값] 목록에 있는 정확한 이름을 쓰세요.
- comparators: 같은 범주에서 '다른 결정'을 받았거나 비교가 필요한 대상입니다.
  예: 베이커리 중 단팥빵만 줄이고 카스텔라·크림빵은 유지 → 단팥빵 claim의 comparators=카스텔라, 크림빵.
  예: 행사 반응이 좋은 품목 유지 vs 효과 없는 품목 제외 → 서로가 비교 대상.
- 데이터와 무관한 결정(진열 위치 변경 등)도 claim으로 남기되 stated_reason을 그대로 적으세요."""

DECOMPOSE_USER = """[보고서] {title} (당월 {period}, 비교월 {prev_period})
[데이터 기간] {months}

[사용 가능한 값]
item_name: {items}
category_l1: {cat1}
category_l2: {cat2}

[제안 목록]
{proposals}"""

INVESTIGATE_SYSTEM = """당신은 보고서 담당자가 어떤 데이터를 보고 이 제안을 했는지 되찾는 조사관입니다.
도구로 근거 가설을 직접 검증하세요. 숫자는 반드시 도구 결과만 사용합니다.

조사 원칙:
1. 보고서에 적힌 근거 표현(stated_reason)에서 가설을 세우세요. '꾸준히/몇 달째' → analyze_trend,
   '유난히 많이 팔린 달' → analyze_baseline, '할인해도 안 늘어'·'행사 반응이 좋았다'·'행사 효과가 없다' → analyze_promo_lift,
   '금요일에 많이' → analyze_dow.
2. 보고서는 당월과 비교월만 보여 줍니다. 담당자는 그보다 긴 기간을 봤을 수 있으니 데이터 전체 기간을 활용하세요.
3. 반드시 비교 대상(comparators)에도 같은 도구를 적용하세요. 대상에는 성립하고 비교 대상에는 성립하지 않아야 근거로 인정됩니다.
   예: '전월 대비 감소'는 카스텔라에도 성립하므로 단팥빵만 줄인 이유가 될 수 없습니다.
4. scope_values에 여러 품목을 한 번에 넣을 수 있습니다. 대상과 비교 대상을 한 번의 호출로 함께 비교하세요.
5. 도구 호출은 제안당 최대 {limit}회입니다. 충분히 확인했으면 도구 없이 짧게 조사 결과를 숫자와 함께 요약하세요.
6. 보고서 당월은 {period}, 비교월은 {prev_period}입니다. 행사 효과·요일 패턴은 당월로 보세요. 부분월({partial})은 근거로 쓰지 마세요."""

INVESTIGATE_USER = """[제안 {key}] {title}
{text}

[검증할 결정]
{claims}

[데이터] dataset_id={dataset_id}, 월별 영업일수 {months}"""

JUDGE_SYSTEM = """조사 결과를 보고 결정(claim)마다 근거를 판정하세요.
- 가설마다 검증한 도구 결과 ID(result_ids)를 적고, 대상에 성립하는지·비교 대상에도 성립하는지 판정하세요.
- status: explained(한 가설이 대상에만 성립해 결정을 설명함) / ambiguous(여러 가설이 똑같이 설명하거나 기준값이 불명확) / unexplained(데이터로 설명 안 됨, 예: 진열 위치 변경).
- explained면 criterion으로 일반화하세요. condition.field는 해당 분석 결과 행에 실제로 있는 필드여야 합니다:
  trend: consecutive_up, consecutive_down, change_pct, last_mom_pct
  baseline: deviation_pct · promo_lift: lift_pct · dow: vs_overall_pct, daily_avg · rank: vs_compare_pct, daily_avg
  threshold는 대상과 비교 대상을 가르는 값으로 정하세요 (예: 단팥빵 3, 카스텔라 1 → consecutive_down >= 3).
- 같은 제안 안에서 반대 결정을 받은 대상이 comparators입니다. 기준(threshold)은 반드시 대상과 비교 대상을 가르는 값이어야 합니다.
- evidence_summary는 숫자를 포함해 1~3줄로 쓰세요 (예: '단팥빵 일평균 6.68→6.5→6.13→5.33, 3개월 연속 감소 / 카스텔라는 1개월 감소').
- cited_numbers에는 판정에 쓴 숫자를 도구 결과에 적힌 그대로 적으세요. 지어낸 숫자는 검증에서 걸러집니다.
- ambiguous/unexplained면 담당자에게 물을 question을 쓰세요. explained여도 기준값이 추정이면 question에 확인 질문을 쓰세요."""

JUDGE_USER = """[제안 {key}] {title}
{text}

[결정]
{claims}

[도구 결과]
{results}

[조사 메모]
{notes}"""

ANSWER_SYSTEM = """담당자의 답변을 판단 기준 한 문장으로 정리하세요. 답변이 데이터로 다시 계산할 수 있는 기준이면 criterion도 채우세요.
condition.field 규칙은 다음과 같습니다. trend: consecutive_up/consecutive_down/change_pct, baseline: deviation_pct, promo_lift: lift_pct, dow: vs_overall_pct.
답변에 없는 내용은 지어내지 마세요."""

MANUAL_SYSTEM = """인수인계 매뉴얼의 서술 부분을 쓰세요. 후임자가 이 보고서를 처음 작성한다고 생각하고 구체적으로 쓰되,
입력에 없는 사실(파일 입수처, 마감일, 담당자 이름 등)은 지어내지 마세요. 숫자와 표는 시스템이 따로 채웁니다."""
