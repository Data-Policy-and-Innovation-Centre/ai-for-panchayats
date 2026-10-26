import requests
from bs4 import BeautifulSoup
from .config import URL, HEADERS, COOKIES


def scrape_panchayat_info(state_id, local_body_type_code, label1, label11, label111):
    """
    Scrapes comprehensive demographic and infrastructural data for a specific panchayat.
    """
    data = {
        "stateId": state_id,
        "localBodyTypeCode": local_body_type_code,
        "label1": label1,
        "label11": label11,
        "label111": label111,
        "village": "",
        "captchaAnswer": "",
    }

    try:
        response = requests.post(URL, headers=HEADERS, cookies=COOKIES, data=data)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        panchayat_data = {}

        # 1. The first table on the page contains the Basic Info (State, District, etc.)
        first_table = soup.find("table")
        if first_table:
            basic_info = {}
            for r in first_table.find_all("tr"):
                c = r.find_all(["th", "td"])
                if len(c) >= 2:
                    key = c[0].text.strip()
                    value = c[1].text.strip()
                    if key and value:
                        basic_info[key] = value
            # Only add if non-empty
            if basic_info:
                panchayat_data["Basic Info"] = basic_info

        # 2. Find all the other sections (Panchayat At a Glance, Demographic Details, etc.)
        for h in soup.find_all("h6"):
            sec = h.text.strip()
            if not sec:
                continue

            p = h.find_parent("div", class_="card-header")
            if p:
                b = p.find_next_sibling("div", class_="card-body")
                if b:
                    tables = b.find_all("table")
                    if tables:
                        sec_data = []
                        for t in tables:
                            t_data = {}
                            for r in t.find_all("tr"):
                                c = r.find_all(["th", "td"])
                                if len(c) >= 2:
                                    key = c[0].text.strip()
                                    value = c[1].text.strip()
                                    if key and value:
                                        t_data[key] = value
                            if t_data:
                                sec_data.append(t_data)

                        # Only add populated sections
                        if len(sec_data) == 1:
                            panchayat_data[sec] = sec_data[0]
                        elif len(sec_data) > 1:
                            panchayat_data[sec] = sec_data

        # Validate that substantive profile data was extracted:
        # A valid profile response must include a populated 'Basic Info' section
        # with actual demographic/identifying attributes (not just an empty section
        # or an error/login response).
        basic = panchayat_data.get("Basic Info")
        if not panchayat_data or not isinstance(basic, dict) or len(basic) < 2:
            preview = soup.get_text()[:500].strip()
            print(f"Warning: No valid panchayat profile data found for {label111}. Page preview: {preview!r}")
            return None

        return panchayat_data

    except requests.exceptions.RequestException as e:
        print(f"Error fetching data for panchayat {label111}: {e}")
        return None
