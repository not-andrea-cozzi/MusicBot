from typing import Dict, Optional

class LocaleManager:
    COUNTRY_LANG_MAP: Dict[str, str] = {
        # Nord America
        "US": "en_us", "CA": "en_ca", "MX": "es_mx",
        
        # Sud e Centro America
        "AR": "es_ar", "BR": "pt_br", "CL": "es_cl", "CO": "es_co",
        "PE": "es_pe", "VE": "es_ve", "EC": "es_ec", "DO": "es_do",
        "PR": "es_pr", "CR": "es_cr", "PA": "es_pa", "UY": "es_uy",
        
        # Europa
        "IT": "it_it", "GB": "en_gb", "IE": "en_ie", "FR": "fr_fr",
        "DE": "de_de", "ES": "es_es", "PT": "pt_pt", "NL": "nl_nl",
        "BE": "fr_be", "CH": "de_ch", "AT": "de_at", "SE": "sv_se",
        "NO": "no_no", "DK": "da_dk", "FI": "fi_fi", "PL": "pl_pl",
        "RU": "ru_ru", "UA": "uk_ua", "GR": "el_gr", "TR": "tr_tr",
        "CZ": "cs_cz", "HU": "hu_hu", "RO": "ro_ro", "BG": "bg_bg",
        "HR": "hr_hr", "SK": "sk_sk", "SI": "sl_si", "IS": "en_is",
        
        # Asia e Oceania
        "AU": "en_au", "NZ": "en_nz", "JP": "ja_jp", "KR": "ko_kr",
        "CN": "zh_cn", "TW": "zh_tw", "HK": "zh_hk", "IN": "en_in",
        "ID": "id_id", "PH": "en_ph", "MY": "en_my", "SG": "en_sg",
        "TH": "th_th", "VN": "vi_vn",
        
        # Africa e Medio Oriente
        "ZA": "en_za", "NG": "en_ng", "EG": "ar_eg", "IL": "he_il",
        "SA": "ar_sa", "AE": "ar_ae", "MA": "ar_ma", "DZ": "ar_dz",
    }

    DETECTED_LANG_MAP: Dict[str, str] = {
        "en": "en_us", "it": "it_it", "es": "es_es", "fr": "fr_fr",
        "de": "de_de", "pt": "pt_pt", "nl": "nl_nl", "sv": "sv_se",
        "no": "no_no", "da": "da_dk", "fi": "fi_fi", "pl": "pl_pl",
        "ru": "ru_ru", "uk": "uk_ua", "ja": "ja_jp", "ko": "ko_kr",
        "zh": "zh_cn", "tr": "tr_tr", "el": "el_gr", "cs": "cs_cz",
        "hu": "hu_hu", "ro": "ro_ro", "id": "id_id", "th": "th_th",
        "vi": "vi_vn", "ar": "ar_sa", "he": "he_il",
    }

    NAME_TO_ISO: Dict[str, str] = {
        # Nord America
        "united states": "US", "usa": "US", "america": "US", "united states of america": "US",
        "canada": "CA", "mexico": "MX",
        
        # Sud e Centro America
        "argentina": "AR", "brazil": "BR", "chile": "CL", "colombia": "CO",
        "peru": "PE", "venezuela": "VE", "ecuador": "EC", "dominican republic": "DO",
        "puerto rico": "PR", "costa rica": "CR", "panama": "PA", "uruguay": "UY",
        "cuba": "CU", "jamaica": "JM", "bolivia": "BO", "paraguay": "PY",
        
        # Europa
        "united kingdom": "GB", "uk": "GB", "great britain": "GB", "england": "GB",
        "italy": "IT", "spain": "ES", "france": "FR", "germany": "DE",
        "ireland": "IE", "republic of ireland": "IE", "portugal": "PT",
        "netherlands": "NL", "holland": "NL", "belgium": "BE", "switzerland": "CH",
        "austria": "AT", "sweden": "SE", "norway": "NO", "denmark": "DK",
        "finland": "FI", "poland": "PL", "russia": "RU", "russian federation": "RU",
        "ukraine": "UA", "greece": "GR", "turkey": "TR", "czech republic": "CZ",
        "czechia": "CZ", "hungary": "HU", "romania": "RO", "bulgaria": "BG",
        "croatia": "HR", "slovakia": "SK", "slovenia": "SI", "serbia": "RS",
        "iceland": "IS", "estonia": "EE", "latvia": "LV", "lithuania": "LT",
        
        # Asia e Oceania
        "australia": "AU", "new zealand": "NZ", "japan": "JP", "south korea": "KR",
        "korea": "KR", "republic of korea": "KR", "china": "CN", "prc": "CN",
        "taiwan": "TW", "hong kong": "HK", "india": "IN", "indonesia": "ID",
        "philippines": "PH", "malaysia": "MY", "singapore": "SG", "thailand": "TH",
        "vietnam": "VN", "pakistan": "PK", "bangladesh": "BD", "sri lanka": "LK",
        "nepal": "NP",
        
        # Africa e Medio Oriente
        "south africa": "ZA", "nigeria": "NG", "egypt": "EG", "israel": "IL",
        "saudi arabia": "SA", "united arab emirates": "AE", "uae": "AE",
        "morocco": "MA", "algeria": "DZ", "kenya": "KE", "ghana": "GH",
        "senegal": "SN", "tunisia": "TN", "lebanon": "LB", "jordan": "JO",
    }

    @classmethod
    def normalize_country_code(cls, country_input: str) -> Optional[str]:
        if not country_input:
            return None
            
        clean_input = country_input.strip()
        
        if len(clean_input) == 2:
            upper_code = clean_input.upper()
            if upper_code in cls.COUNTRY_LANG_MAP:
                return upper_code
                
        return cls.NAME_TO_ISO.get(clean_input.lower())