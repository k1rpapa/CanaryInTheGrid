import os
import json
import time
import requests
import gspread
import statistics
from google.oauth2.service_account import Credentials
from google.cloud import firestore
from datetime import datetime, timezone, timedelta

# =========================================================================
# CanaryInTheGrid v4.6.1 - Phase 3: CFTC Macro Proxy (Endpoint Fix)
# =========================================================================

ALL_MONTHS = list(range(0, 25))
FAR_MONTHS = list(range(12, 25))

def fetch_with_backoff(url, headers=None, max_retries=5):
    delay = 1
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            return response
        except (requests.RequestException, Exception) as e:
            if attempt == max_retries - 1:
                print(f"[-] Fatal: API Request failed: {e}")
                raise e
            time.sleep(delay)
            delay *= 2

def safe_int(value):
    if not value or value == '-': return 0
    if isinstance(value, str):
        return int(value.replace(',', '').split('.')[0])
    return int(value)

def fetch_cftc_macro_proxy():
    """CFTC APIからマクロ・プロキシ（ヘンリーハブ天然ガス）の実需＆SDロングを取得"""
    print("[*] Infiltrating CFTC Socrata API for Macro Proxy (Henry Hub)...")
    
    # 修正: CFTC Disaggregated Futures Only Reportの正しいエンドポイントID (72hh-3qpy)
    url = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
    
    # Henry Hub Natural Gas (NYMEX) のCFTCコントラクトコード
    macro_proxy_code = "023651" 
    
    try:
        # 最新のレポートを1件だけ取得
        query = f"{url}?cftc_contract_market_code={macro_proxy_code}&$order=report_date_as_yyyy_mm_dd DESC&$limit=1"
        response = requests.get(query, timeout=10)
        
        if response.status_code == 200 and len(response.json()) > 0:
            data = response.json()[0]
            report_date = data.get("report_date_as_yyyy_mm_dd", "N/A")[:10]
            
            # 天然ガス市場における実需(Producer/Merchant)とSwap Dealersの買いポジション(ロング)を合算
            pm_long = float(data.get("prod_merc_positions_long", 0))
            sd_long = float(data.get("swap_positions_long", 0))
            whale_long_total = pm_long + sd_long
            
            print(f"[+] CFTC Macro Proxy Identified (Date: {report_date}): {whale_long_total} contracts")
            return whale_long_total, report_date
        else:
            raise Exception(f"API returned empty data or status code {response.status_code}.")
            
    except Exception as e:
        print(f"[!] CFTC Fetch Error: {e}. Engaging simulated ledger.")
        return 12500, "Simulated"

def fetch_cme_curve(product_id, default_start_price, default_step):
    print(f"[*] Extracting Price, Volume & OI from Product ID: {product_id}...")
    cme_url = f"https://www.cmegroup.com/CmeWS/mvc/Quotes/Future/{product_id}/G"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.cmegroup.com/"
    }
    
    curve = {}
    try:
        response = fetch_with_backoff(cme_url, headers=headers)
        data = response.json()
        if len(data) < 25: raise Exception(f"Insufficient liquidity ({len(data)} months).")
            
        for idx, m in enumerate(ALL_MONTHS):
            month_data = data[idx]
            price = float(month_data.get('last', 0)) or float(month_data.get('priorSettle', 0))
            curve[m] = {
                'price': price if price > 0.5 else (default_start_price + idx * default_step),
                'volume': safe_int(month_data.get('volume', 0)),
                'oi': safe_int(month_data.get('openInterest', 0))
            }
        return curve
    except Exception as e:
        print(f"[-] CME Extraction Error for {product_id}: {e}")
        return None

def generate_regional_power_curves(gas_curve):
    pjm_curve, ercot_curve = {}, {}
    for m in ALL_MONTHS:
        pjm_base_hr = 7.6 + ((m - 12) * 0.05) if m >= 12 else 7.3 + (m * 0.02)
        ercot_base_hr = 6.2 + ((m - 12) * 0.02) if m >= 12 else 6.0 + (m * 0.01)
        dummy_oi = max(0, 500 - (m * 15)) 
        
        pjm_curve[m] = {'price': round(gas_curve[m]['price'] * pjm_base_hr, 2), 'volume': 0, 'oi': dummy_oi}
        ercot_curve[m] = {'price': round(gas_curve[m]['price'] * ercot_base_hr, 2), 'volume': 0, 'oi': dummy_oi}
    return pjm_curve, ercot_curve

def fetch_market_data_with_fallback():
    gas_curve = fetch_cme_curve("425", 2.50, 0.02)
    if not gas_curve:
        gas_curve = {m: {'price': 2.50 + (m * 0.02), 'volume': 0, 'oi': 0} for m in ALL_MONTHS}
        
    pjm_curve = fetch_cme_curve("324", 45.0, 0.5)
    ercot_curve = fetch_cme_curve("838", 35.0, 0.2)
    
    if not pjm_curve or not ercot_curve:
        print("[!] Tactical Fallback (Modeling) engaged.")
        pjm_curve, ercot_curve = generate_regional_power_curves(gas_curve)
        
    return gas_curve, pjm_curve, ercot_curve

def calculate_macro_metrics(gas_curve, pjm_curve, ercot_curve, cftc_whale_long, cftc_date):
    print("[*] Calculating Phase 1-3 Unified Metrics...")
    
    pjm_all_hrs, ercot_all_hrs, spreads = [], [], []
    curve_data_map = {}
    pjm_far_oi_total = 0
    pjm_far_vol_total = 0
    
    for m in ALL_MONTHS:
        g_price = gas_curve[m]['price']
        p_pjm = pjm_curve[m]['price']
        p_ercot = ercot_curve[m]['price']
        
        hr_pjm = p_pjm / g_price if g_price > 0.1 else 7.5
        hr_ercot = p_ercot / g_price if g_price > 0.1 else 6.0
        
        if hr_pjm < 1.0 or hr_pjm > 30.0: hr_pjm = 7.5
        if hr_ercot < 1.0 or hr_ercot > 30.0: hr_ercot = 6.0
        
        pjm_all_hrs.append(hr_pjm)
        ercot_all_hrs.append(hr_ercot)
        
        spread_pct = ((hr_pjm - hr_ercot) / hr_ercot) * 100 if hr_ercot > 0 else 0
        spreads.append(spread_pct)
        
        if m in FAR_MONTHS:
            pjm_far_oi_total += pjm_curve[m]['oi']
            pjm_far_vol_total += pjm_curve[m]['volume']
        
        curve_data_map[f"pjm_hr_{m}m"] = round(hr_pjm, 4)
        curve_data_map[f"ercot_hr_{m}m"] = round(hr_ercot, 4)
        curve_data_map[f"pjm_oi_{m}m"] = pjm_curve[m]['oi']
        curve_data_map[f"pjm_vol_{m}m"] = pjm_curve[m]['volume']
        
    pjm_far_hrs = pjm_all_hrs[12:25]
    spreads_far = spreads[12:25]
    
    band_mean_far_hr = statistics.mean(pjm_far_hrs)
    band_mean_near_hr = statistics.mean(pjm_all_hrs[0:12])
    band_mean_far_spread = statistics.mean(spreads_far)
    slope, _ = statistics.linear_regression(FAR_MONTHS, pjm_far_hrs)
    
    LIQUIDITY_WARNING = pjm_far_oi_total < 500
    WHALE_WARNING = cftc_whale_long < 100000 
    
    if band_mean_far_hr < 7.0 and slope < 0 and LIQUIDITY_WARNING and WHALE_WARNING:
        state = "🔴 【崩壊確定】OI消失・マクロ実需逃避。バブル完全崩壊"
    elif slope < 0 or (band_mean_far_spread < 15.0 and LIQUIDITY_WARNING):
        state = "🟠 【真空状態】流動性枯渇・スプレッド急縮小"
    elif band_mean_far_hr < 7.0:
        state = "🟡 【偽陽性】水準低下するもOI維持(ノイズ)"
    elif LIQUIDITY_WARNING or WHALE_WARNING:
        state = "🟡 【流動性低下】価格維持するもマクロ実需建玉減少(警戒)"
    else:
        state = "🟢 【正常】AIプレミアム＆マクロ実需OI 堅調維持"

    return {
        "timestamp_jst": (datetime.now(timezone.utc) + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M:%S'),
        "slope": round(slope, 6),
        "band_mean_hr": round(band_mean_far_hr, 4),
        "band_mean_near_hr": round(band_mean_near_hr, 4),
        "band_mean_spread": round(band_mean_far_spread, 2),
        "far_oi_total": pjm_far_oi_total,
        "far_vol_total": pjm_far_vol_total,
        "cftc_whale_long": cftc_whale_long,
        "cftc_date": cftc_date,
        "state": state,
        "curve_data": curve_data_map
    }

def log_to_google_sheets(metrics, credentials_json_str, sheet_id):
    print("[*] Logging to Google Sheets...")
    try:
        credentials_info = json.loads(credentials_json_str)
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        credentials = Credentials.from_service_account_info(credentials_info, scopes=scopes)
        gc = gspread.authorize(credentials)
        worksheet = gc.open_by_key(sheet_id).sheet1
        
        row_data = [
            metrics["timestamp_jst"], metrics["band_mean_hr"], metrics["slope"],
            metrics["band_mean_spread"], metrics["band_mean_near_hr"], 
            metrics["far_oi_total"], metrics["far_vol_total"], 
            metrics["cftc_whale_long"], metrics["state"]
        ]
        worksheet.append_row(row_data)
        print("[+] Google Sheets sync complete.")
    except Exception as e: print(f"[-] Sheets Logging Error: {e}")

def log_to_cloud_firestore(metrics, credentials_json_str):
    print("[*] Logging to Cloud Firestore...")
    try:
        credentials_info = json.loads(credentials_json_str)
        credentials = Credentials.from_service_account_info(credentials_info)
        db = firestore.Client(credentials=credentials, project=credentials_info['project_id'])
        
        doc_id = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        db.collection("canary_logs").document(doc_id).set(metrics)
        print(f"[+] Firestore sync complete. DocID: {doc_id}")
    except Exception as e: print(f"[-] Firestore Logging Error: {e}")

if __name__ == "__main__":
    GCP_SHEET_ID = os.getenv("GCP_SHEET_ID")
    GCP_SERVICE_ACCOUNT_JSON = os.getenv("GCP_SERVICE_ACCOUNT_JSON")
    
    if not GCP_SHEET_ID or not GCP_SERVICE_ACCOUNT_JSON:
        print("[-] Fatal: Missing environment variables.")
        exit(1)

    cftc_whale_long, cftc_date = fetch_cftc_macro_proxy()
    gas_curve, pjm_curve, ercot_curve = fetch_market_data_with_fallback()
    
    metrics = calculate_macro_metrics(gas_curve, pjm_curve, ercot_curve, cftc_whale_long, cftc_date)
    
    log_to_google_sheets(metrics, GCP_SERVICE_ACCOUNT_JSON, GCP_SHEET_ID)
    log_to_cloud_firestore(metrics, GCP_SERVICE_ACCOUNT_JSON)
    
    print("[+] Canary Monitor Execution Finished Successfully.")
