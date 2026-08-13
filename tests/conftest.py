"""Synthetic fixtures for the panchayat read model.

Tiny, fully invented data with the same shape and the same awkward edges as the
real extracts: codes that pandas wants to make floats, pipe-delimited voucher
lists, a voucher paid in a later year than its plan, and a deliberate orphan.

No test reads data/, touches a network, or needs a real extract.
"""

from __future__ import annotations

import pandas as pd
import pytest

from database.build import Sources
from database.transform import NSAP_COLUMNS, SATELLITES


@pytest.fixture
def planning_csv() -> pd.DataFrame:
    """Two activities in one plan, plus the satellite and NSAP columns."""
    rows = [
        {"gp_lgd_code": 119598, "plan_code": 6012003, "activity_code": 128856295,
         "plan_year": 2024, "source_file": "pl.json", "activity_type": 1,
         "activity_name": "Pipe conveyance system", "activity_desc": "desc",
         "focus_area": 7, "activity_for": 2, "work_type": 3,
         "is_costless_activity": 0, "total_cost": 97000.00,
         "operation_type": None, "operation_remarks": None, "output_type": 1,
         "activity_status": 4},
        {"gp_lgd_code": 119598, "plan_code": 6012003, "activity_code": 128856619,
         "plan_year": 2024, "source_file": "pl.json", "activity_type": 1,
         "activity_name": "Retaining wall", "activity_desc": "desc",
         "focus_area": 8, "activity_for": 2, "work_type": 3,
         "is_costless_activity": 0, "total_cost": 103847.00,
         "operation_type": None, "operation_remarks": None, "output_type": 1,
         "activity_status": 4},
    ]
    frame = pd.DataFrame(rows)
    for columns in SATELLITES.values():
        for column in columns:
            if column not in frame:
                frame[column] = None
    for column in NSAP_COLUMNS:
        frame[column] = None
    return frame


@pytest.fixture
def expenditure_csv() -> pd.DataFrame:
    """Expenditure with pipe-delimited voucher lists, in the source's naming."""
    return pd.DataFrame([
        {"planYear": 2024, "stateName": "Odisha", "zpName": "Khordha",
         "blockName": "Bhubaneswar", "gpName": "Andhrua", "gpCode": 119598,
         "planType": "GPDP", "approvalDate": "2024-04-01", "planCode": 6012003,
         "S.No.": 1, "Activity Code": 128856295, "Activity Name": "Pipe",
         "Activity For": 2, "Focus Area": 7,
         "Approved Cost in Action Plan": 97000.00,
         "Technical Approved Cost": 97000.00, "Admin Approved Cost": 97000.00,
         "Scheme Name": "XVFC", "General": 50000.00, "SC": 0.00, "ST": 0.00,
         "Total Expenditure": 50000.00,
         # Paid in 2025-26 although planned in 2024: the bridge must use the
         # voucher's own year, not the plan's.
         "Voucher No": "XVFC/2025-26/P/143 | XVFC/2025-26/P/144",
         "Voucher Date": "05/06/2025 | 07/06/2025",
         "Voucher Cost": "30000.00 | 20000.00"},
        {"planYear": 2024, "stateName": "Odisha", "zpName": "Khordha",
         "blockName": "Bhubaneswar", "gpName": "Andhrua", "gpCode": 119598,
         "planType": "GPDP", "approvalDate": "2024-04-01", "planCode": 6012003,
         "S.No.": 2, "Activity Code": 128856619, "Activity Name": "Wall",
         "Activity For": 2, "Focus Area": 8,
         "Approved Cost in Action Plan": 103847.00,
         "Technical Approved Cost": None, "Admin Approved Cost": None,
         "Scheme Name": "XVFC", "General": 25000.00, "SC": 0.00, "ST": 0.00,
         "Total Expenditure": 25000.00,
         "Voucher No": None, "Voucher Date": None, "Voucher Cost": None},
    ])


@pytest.fixture
def vouchers_csv() -> pd.DataFrame:
    return pd.DataFrame([
        {"gp_lgd_code": 119598, "gp_name": "Andhrua", "state": 21,
         "district": 321, "block": 3823, "fiscal_year": "2025-2026",
         "voucher_no": "XVFC/2025-26/P/143", "voucher_id": "V1",
         "direction": "payment", "type": "P", "date": "05/06/2025",
         "month": "June", "amount": 30000.00},
        {"gp_lgd_code": 119598, "gp_name": "Andhrua", "state": 21,
         "district": 321, "block": 3823, "fiscal_year": "2025-2026",
         "voucher_no": "XVFC/2025-26/P/144", "voucher_id": "V1",
         "direction": "payment", "type": "P", "date": "07/06/2025",
         "month": "June", "amount": 20000.00},
    ])


@pytest.fixture
def admin_approval_csv() -> pd.DataFrame:
    return pd.DataFrame([
        {"row_id": "119598|2024|AA|0", "lgd_code": 119598,
         "gram_panchayat_name": "Andhrua", "plan_year": "2024",
         "doc_type": "AA", "source_file": "aa.json", "activityCd": 128856295,
         "wrkPlnYr": "2024", "wrkAdmApprNo": "007",
         "wrkAdmApprSnctnOrdrDt": "2024-05-01", "wrkProposedCost": 97000.00,
         "wrkAdmApprIssAuthrty": "BDO"},
    ])


@pytest.fixture
def admin_approval_scheme_csv() -> pd.DataFrame:
    return pd.DataFrame([
        {"row_id": "119598|2024|AA|0/schemes:0",
         "parent_row_id": "119598|2024|AA|0", "pos": 0,
         "activityCd": 128856295, "wrkSchmCd": 12, "wrkSchmCmpntCd": 3,
         "wrkAdmApprFndSnctnGen": 97000.00, "wrkAdmApprFndSnctnSc": 0.00,
         "wrkAdmApprFndSnctnSt": 0.00, "wrkAdmApprFndSnctnTotal": 97000.00},
    ])


@pytest.fixture
def technical_approval_csv() -> pd.DataFrame:
    return pd.DataFrame([
        {"row_id": "119598|2024|TA|0", "lgd_code": 119598,
         "gram_panchayat_name": "Andhrua", "plan_year": "2024",
         "doc_type": "TA", "source_file": "ta.json", "activityCd": 128856295,
         "wrkTecApprReqFlg": "Y", "wrkTecApprCost": 97000.00,
         "wrkTecApprIssAuthrty": "JE", "wrkTecApprOrdrNo": "0012",
         "wrkTecApprOrdrDt": "2024-05-10"},
    ])


@pytest.fixture
def physical_progress_csv() -> pd.DataFrame:
    """One single-capture row and one holding two comma-separated captures."""
    return pd.DataFrame([
        {"row_id": "119598|2024|PP|0", "parent_row_id": "119598|2024|PP",
         "pos": 0, "activityCd": 128856295, "fileUploadId": 5001,
         "longitude": "85.8245", "latitude": "20.2961",
         "plnunttypecode": 3},
        {"row_id": "119598|2024|PP|1", "parent_row_id": "119598|2024|PP",
         "pos": 1, "activityCd": 128856295, "fileUploadId": 5002,
         "longitude": "85.8245,85.8250", "latitude": "20.2961,20.2970",
         "plnunttypecode": 3},
    ])


@pytest.fixture
def code_descriptions_csv() -> pd.DataFrame:
    return pd.DataFrame([
        {"variable": "focus_area", "variabe_codes": 7.0,
         "codes_desc": "Sanitation", "source": "manual", "confidence": "high"},
        {"variable": "focus_area", "variabe_codes": 8.0,
         "codes_desc": "Land improvement", "source": "manual",
         "confidence": "high"},
    ])


@pytest.fixture
def sources(planning_csv, expenditure_csv, vouchers_csv, admin_approval_csv,
            admin_approval_scheme_csv, technical_approval_csv,
            physical_progress_csv, code_descriptions_csv) -> Sources:
    return Sources(
        planning=planning_csv,
        expenditure=expenditure_csv,
        vouchers=vouchers_csv,
        admin_approval=admin_approval_csv,
        admin_approval_scheme=admin_approval_scheme_csv,
        technical_approval=technical_approval_csv,
        physical_progress=physical_progress_csv,
        code_descriptions=code_descriptions_csv,
        welfare_schemes=pd.DataFrame(
            [{"scheme_code": 12.0, "scheme_name": "XVFC"}]),
        lsdg_themes=pd.DataFrame([
            {"focus area": "Sanitation", "dominant LSDG theme": "Clean Village",
             "distinct themes seen": 1.0, "rows": 10.0}]),
    )
