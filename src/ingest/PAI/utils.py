import re

from bs4 import BeautifulSoup

from .config import THEME_COLUMNS


def clean_name(raw_name: str) -> str:
    """Strip trailing LGD code like ' [344]' from names."""
    return re.sub(r"\s*\[\d+\]\s*$", "", raw_name).strip()


def extract_asp_hidden_fields(soup: BeautifulSoup) -> dict:
    """Extract __VIEWSTATE and other hidden fields from the page."""
    fields = {}
    for inp in soup.find_all("input", attrs={"type": "hidden"}):
        name = inp.get("name")
        value = inp.get("value", "")
        if name:
            fields[name] = value
    return fields


def build_form_data(hidden_fields: dict,
                    state_id: str,
                    district_id: str,
                    block_id: str,
                    fy_id: str) -> dict:
    """Build the full POST payload to search GP scores for a block."""
    form = dict(hidden_fields)

    # Dropdowns
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$ddl_State"] = state_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$ddl_District"] = district_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$ddl_Block"] = block_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$ddl_Survey"] = fy_id

    # Hidden id mirrors
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$hdn_FYID"] = fy_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$hdn_StateID"] = state_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$hdn_DistrictID"] = district_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$hdn_BlockID"] = block_id

    # Submit button
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$btnSubmit"] = "Search"

    # Remove event targets for a clean submit
    form["__EVENTTARGET"] = ""
    form["__EVENTARGUMENT"] = ""

    return form


def build_next_page_form(hidden_fields: dict,
                         state_id: str,
                         district_id: str,
                         block_id: str,
                         fy_id: str) -> dict:
    """Build the POST payload for clicking the 'Next 100' button."""
    form = dict(hidden_fields)

    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$ddl_State"] = state_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$ddl_District"] = district_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$ddl_Block"] = block_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$ddl_Survey"] = fy_id

    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$hdn_FYID"] = fy_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$hdn_StateID"] = state_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$hdn_DistrictID"] = district_id
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$hdn_BlockID"] = block_id

    # Click "Next 100 >>"
    form["ctl00$ctl00$CPHRootMidContent$CPHMidContent$btnNext"] = "Next 100 >>"
    form["__EVENTTARGET"] = ""
    form["__EVENTARGUMENT"] = ""

    # Remove the Search button if present
    form.pop("ctl00$ctl00$CPHRootMidContent$CPHMidContent$StateDistrictBlockGP$btnSubmit", None)

    return form


def parse_gp_table(soup: BeautifulSoup,
                   district_name: str,
                   block_name: str):
    """Parse the GP data table and return a list of GP records."""
    table = soup.find("table", {"id": "GVdataT"})
    if not table:
        return []

    tbody = table.find("tbody")
    if not tbody:
        return []

    rows = tbody.find_all("tr")
    records = []

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 11:
            continue

        first_cell = cells[0]
        gp_name = ""
        gp_code = ""

        link = first_cell.find("a")
        if link:
            text = link.get_text(strip=True)
            match = re.match(r"(.+?)-\[(\d+)\]", text)
            if match:
                gp_name = match.group(1).strip()
                gp_code = match.group(2).strip()
            else:
                gp_name = text

        scores = {}
        for i, col_name in enumerate(THEME_COLUMNS):
            cell_text = cells[i + 1].get_text(strip=True)
            try:
                scores[col_name] = float(cell_text)
            except (ValueError, IndexError):
                scores[col_name] = None

        record = {
            "state": "Odisha",
            "district": district_name,
            "block": block_name,
            "gram_panchayat": gp_name,
            "gp_code": gp_code,
            **scores,
        }
        records.append(record)

    return records


def has_next_page(soup: BeautifulSoup) -> bool:
    """Check if the 'Next 100 >>' button is enabled."""
    btn = soup.find("input", {"id": "btnNext"})
    if btn and not btn.get("disabled"):
        return True
    return False
