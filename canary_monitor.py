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
# CanaryInTheGrid v3.7.5 - Full-Curve (0M-24M) Sniper Engine
# =========================================================================

# 0M(当月)から24M先までの全25に限月へ観測窓を拡張
ALL_MONTHS = list(range(0, 25))
FAR_MONTHS = list(range(12, 25)) # 判定用の遠月バンド(12-24M)

def fetch_with_backoff(url, headers=None, max_retries=5):
    delay = 1
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            return response
        except (requests.RequestException, Exception) as e:
            if attempt == max_retries - 1: raise e
            time.sleep(delay)
            delay *= 2

def fetch_cme_curve(product_id, default_start_price, default_step):
    cme_url = f"https://www.cmegroup.com/CmeWS/mvc/Quotes/Future/{product_id}/G"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Referer": "https://www.cmegroup.com/"}
    curve = {}
    try:
        response = fetch_with_backoff(cme_url, headers=headers)
        data = response.json()
        if len(data) < 25: raise Exception("Insufficient liquidity.")
        for idx, m in enumerate(ALL_MONTHS):
            month_data = data[idx] # 0Mから順番に取得
            price = float(month_data.get('last', 0)) or float(month_data.get('priorSettle', 0))
            curve[m] = price if price > 0.5 else (default_start_price + idx * default_step)
        return curve
    except Exception as e:
        print(f"[-] CME Fetch Error for {product_id}: {e}")
        return None

def generate_regional_power_curves(gas_curve):
    pjm_curve, ercot_curve = {}, {}
    for m in ALL_MONTHS:
        pjm_base_hr = 7.6 + ((m - 12) * 0.05) if m >= 12 else 7.3 + (m * 0.02)
        pjm_curve[m] = round(gas_curve[m] * pjm_base_hr, 2)
        ercot_base_hr = 6.2 + ((m - 12) * 0.02) if m >= 12 else 6.0 + (m * 0.01)
        ercot_curve[m] = round(gas_curve[m] * ercot_base_hr, 2)
    return pjm_curve, ercot_curve

def fetch_market_data_with_fallback():
    gas_curve = fetch_cme_curve("425", 2.50, 0.02)
    if not gas_curve: gas_curve = {m: 2.50 + (m * 0.02) for m in ALL_MONTHS}
    pjm_curve = fetch_cme_curve("324", 45.0, 0.5)
    ercot_curve = fetch_cme_curve("838", 35.0, 0.2)
    if not pjm_curve or not ercot_curve:
        pjm_curve, ercot_curve = generate_regional_power_curves(gas_curve)
    return gas_curve, pjm_curve, ercot_curve

def calculate_macro_metrics(gas_curve, pjm_curve, ercot_curve):
    pjm_all_hrs, ercot_all_hrs, spreads = [], [], []
    curve_data_map = {}
    
    for m in ALL_MONTHS:
        g_price = gas_curve[m]
        hr_pjm = pjm_curve[m] / g_price if g_price > 0.1 else 7.5
        hr_ercot = ercot_curve[m] / g_price if g_price > 0.1 else 6.0
        
        # 異常値ガード
        if hr_pjm < 1.0 or hr_pjm > 30.0: hr_pjm = 7.5
        if hr_ercot < 1.0 or hr_ercot > 30.0: hr_ercot = 6.0
        
        pjm_all_hrs.append(hr_pjm)
        ercot_all_hrs.append(hr_ercot)
        
        spread_pct = ((hr_pjm - hr_ercot) / hr_ercot) * 100 if hr_ercot > 0 else 0
        spreads.append(spread_pct)
        
        curve_data_map[f"pjm_hr_{m}m"] = round(hr_pjm, 4)
        curve_data_map[f"ercot_hr_{m}m"] = round(hr_ercot, 4)
        curve_data_map[f"spread_{m}m"] = round(spread_pct, 2)
        
    # 【判定ロジック】あえてブレの少ない「遠月(12-24M)」のデータのみを抽出して演算
    pjm_far_hrs = pjm_all_hrs[12:25]
    spreads_far = spreads[12:25]
    
    band_mean_far_hr = statistics.mean(pjm_far_hrs)
    band_mean_near_hr = statistics.mean(pjm_all_hrs[0:12]) # 参考用の期近平均(0-11M)
    band_mean_far_spread = statistics.mean(spreads_far)
    slope, _ = statistics.linear_regression(FAR_MONTHS, pjm_far_hrs)
    
    if band_mean_far_hr < 7.0 and band_mean_far_spread < 15.0 and slope < 0:
        state = "🔴 【崩壊確定】AIプレミアム完全剥落・逆サヤ化"
    elif slope < 0 or band_mean_far_spread < 15.0:
        state = "🟠 【真空状態】スプレッド縮小・カーブ水平化"
    elif band_mean_far_hr < 7.0:
        state = "🟡 【偽陽性】絶対水準低下(全体ノイズ)"
    else:
        state = "🟢 【正常】AIプレミアム維持・順サヤ巡航"

    return {
        "timestamp_jst": (datetime.now(timezone.utc) + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M:%S'),
        "slope": round(slope, 6),
        "band_mean_hr": round(band_mean_far_hr, 4),      # 判定用(遠月)
        "band_mean_near_hr": round(band_mean_near_hr, 4), # 参考用(期近)
        "band_mean_spread": round(band_mean_far_spread, 2),
        "state": state,
        "curve_data": curve_data_map
    }

def log_to_google_sheets(metrics, credentials_json_str, sheet_id):
    try:
        credentials_info = json.loads(credentials_json_str)
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        credentials = Credentials.from_service_account_info(credentials_info, scopes=scopes)
        worksheet = gspread.authorize(credentials).open_by_key(sheet_id).sheet1
        row_data = [
            metrics["timestamp_jst"], metrics["band_mean_hr"], metrics["slope"],
            metrics["band_mean_spread"], metrics["band_mean_near_hr"], metrics["state"]
        ]
        worksheet.append_row(row_data)
    except Exception as e: print(f"[-] Sheets Error: {e}")

def log_to_cloud_firestore(metrics, credentials_json_str):
    try:
        credentials_info = json.loads(credentials_json_str)
        credentials = Credentials.from_service_account_info(credentials_info)
        db = firestore.Client(credentials=credentials, project=credentials_info['project_id'])
        doc_id = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        db.collection("canary_logs").document(doc_id).set(metrics)
    except Exception as e: print(f"[-] Firestore Error: {e}")

if __name__ == "__main__":
    GCP_SHEET_ID = os.getenv("GCP_SHEET_ID")
    GCP_SERVICE_ACCOUNT_JSON = os.getenv("GCP_SERVICE_ACCOUNT_JSON")
    if GCP_SHEET_ID and GCP_SERVICE_ACCOUNT_JSON:
        gas, pjm, ercot = fetch_market_data_with_fallback()
        metrics = calculate_macro_metrics(gas, pjm, ercot)
        log_to_google_sheets(metrics, GCP_SERVICE_ACCOUNT_JSON, GCP_SHEET_ID)
        log_to_cloud_firestore(metrics, GCP_SERVICE_ACCOUNT_JSON)
        print("[+] Execution Finished Successfully.")
