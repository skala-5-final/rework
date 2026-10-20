def test_report_period(report):
    assert report["period"] == "2026-06"
    assert report["prev_period"] == "2026-05"


def test_tables_restored(report):
    kpi = next(t for t in report["tables"] if t["title"] == "주요 지표")
    assert kpi["columns"] == ["구분", "5월", "6월", "증감률", "단위"]
    assert kpi["rows"][0][:3] == ["매출액", "18,595", "18,199"]
    assert len(report["tables"]) == 6


def test_proposals_with_wrapped_cells(report):
    """여러 줄로 감싸진 제안 칸도 행 구분선 기준으로 한 제안으로 묶인다."""
    assert len(report["proposals"]) == 5
    assert report["proposals"][3]["title"].startswith("베이커리")
    assert "단팥빵" in report["proposals"][3]["text"]
