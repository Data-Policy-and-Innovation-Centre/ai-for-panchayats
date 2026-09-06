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
        "captchaAnswer": ""
    }

    try:
        response = requests.post(URL, headers=HEADERS, cookies=COOKIES, data=data)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        panchayat_data = {}
        
        # 1. The first table on the page contains the Basic Info (State, District, etc.)
        first_table = soup.find('table')
        if first_table:
            basic_info = {}
            for r in first_table.find_all('tr'):
                c = r.find_all(['th','td'])
                if len(c) >= 2:
                    key = c[0].text.strip()
                    value = c[1].text.strip()
                    if key:
                        basic_info[key] = value
            panchayat_data['Basic Info'] = basic_info

        # 2. Find all the other sections (Panchayat At a Glance, Demographic Details, etc.)
        for h in soup.find_all('h6'):
            sec = h.text.strip()
            panchayat_data[sec] = {}
            
            p = h.find_parent('div', class_='card-header')
            if p:
                b = p.find_next_sibling('div', class_='card-body')
                if b:
                    tables = b.find_all('table')
                    if tables:
                        sec_data = []
                        for t in tables:
                            t_data = {}
                            # In some sections, data is represented row-by-row
                            for r in t.find_all('tr'):
                                c = r.find_all(['th','td'])
                                if len(c) >= 2:
                                    key = c[0].text.strip()
                                    value = c[1].text.strip()
                                    if key:
                                        t_data[key] = value
                            if t_data:
                                sec_data.append(t_data)
                        
                        # If a section only has one table, unnest it for cleaner JSON
                        if len(sec_data) == 1:
                            panchayat_data[sec] = sec_data[0]
                        else:
                            panchayat_data[sec] = sec_data
        
        # If no data at all was found, maybe grab all text for debugging
        if not panchayat_data:
            panchayat_data["raw_content_preview"] = soup.get_text()[:500]
            
        return panchayat_data

    except requests.exceptions.RequestException as e:
        print(f"Error fetching data for panchayat {label111}: {e}")
        return None
