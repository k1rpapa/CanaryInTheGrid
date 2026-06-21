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
# CanaryInTheGrid v4.0 - Liquid Gravity Engine (Phase 2)
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
    """CMEのカンマ区切り文字列(例:'1,250')を安全に数値へ変換"""
    if not value or value == '-': return 0
    if isinstance(value, str):
        return int(value.replace(',', '').split('.')[0])
    return int(value)

def fetch_cme_curve(product_id, default_start_price, default_step):
    """CMEから価格だけでなくVolumeとOI(建玉)も強制抽出するスナイパー"""
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
            volume = safe_int(month_data.get('volume', 0))
            oi = safe_int(month_data.get('openInterest', 0))
            
            curve[m] = {
                'price': price if price > 0.5 else (default_start_price + idx * default_step),
                'volume': volume,
                'oi': oi
            }
        return curve
    except Exception as e:
        print(f"[-] CME Extraction Error for {product_id}: {e}")
        return None

def generate_regional_power_curves(gas_curve):
    """流動性枯渇時のフォールバック（OI/Volumeのダミー生成含む）"""
    pjm_curve, ercot_curve = {}, {}
    for m in ALL_MONTHS:
        # 価格モデリング
        pjm_base_hr = 7.6 + ((m - 12) * 0.05) if m >= 12 else 7.3 + (m * 0.02)
        ercot_base_hr = 6.2 + ((m - 12) * 0.02) if m >= 12 else 6.0 + (m * 0.01)
        
        # 枯渇時を想定した低いOIダミー
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

def calculate_macro_metrics(gas_curve, pjm_curve, ercot_curve):
    """価格空間(Phase1)と流動性重力(Phase2)の統合判定"""
    print("[*] Calculating Phase 1 & Phase 2 Metrics...")
    
    pjm_all_hrs, ercot_all_hrs, spreads = [], [], []
    curve_data_map = {}
    
    # Phase 2: 流動性集計用変数
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
        
        # 遠月(12-24M)のOIとVolumeを合算（PJM市場のクジラの生息確認）
        if m in FAR_MONTHS:
            pjm_far_oi_total += pjm_curve[m]['oi']
            pjm_far_vol_total += pjm_curve[m]['volume']
        
        # UI用に価格、OI、Volumeをすべて保存
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
    
    # =====================================================
    # 🚨 Phase 2 判定ゲート (流動性枯渇 + 逆サヤ)
    # OI合計が極端に少ない(例:100未満)場合は、価格が見せかけと判断
    # =====================================================
    LIQUIDITY_WARNING = pjm_far_oi_total < 500  # 仮のしきい値(実稼働で調整)
    
    if band_mean_far_hr < 7.0 and slope < 0 and LIQUIDITY_WARNING:
        state = "🔴 【崩壊確定】OI消失・逆サヤ化。バブル完全崩壊"
    elif slope < 0 or (band_mean_far_spread < 15.0 and LIQUIDITY_WARNING):
        state = "🟠 【真空状態】流動性枯渇・スプレッド急縮小"
    elif band_mean_far_hr < 7.0:
        state = "🟡 【偽陽性】水準低下するもOI維持(ノイズ)"
    elif LIQUIDITY_WARNING:
        state = "🟡 【流動性低下】価格維持するも建玉減少(警戒)"
    else:
        state = "🟢 【正常】AIプレミアム＆OI(建玉) 堅調維持"

    return {
        "timestamp_jst": (datetime.now(timezone.utc) + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M:%S'),
        "slope": round(slope, 6),
        "band_mean_hr": round(band_mean_far_hr, 4),
        "band_mean_near_hr": round(band_mean_near_hr, 4),
        "band_mean_spread": round(band_mean_far_spread, 2),
        "far_oi_total": pjm_far_oi_total,   # NEW: 遠月の建玉総量
        "far_vol_total": pjm_far_vol_total, # NEW: 遠月の出来高総量
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
        
        # OIとVolumeをシートの列に追加
        row_data = [
            metrics["timestamp_jst"], metrics["band_mean_hr"], metrics["slope"],
            metrics["band_mean_spread"], metrics["band_mean_near_hr"], 
            metrics["far_oi_total"], metrics["far_vol_total"], metrics["state"]
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

    gas_curve, pjm_curve, ercot_curve = fetch_market_data_with_fallback()
    metrics = calculate_macro_metrics(gas_curve, pjm_curve, ercot_curve)
    
    log_to_google_sheets(metrics, GCP_SERVICE_ACCOUNT_JSON, GCP_SHEET_ID)
    log_to_cloud_firestore(metrics, GCP_SERVICE_ACCOUNT_JSON)
    
    print("[+] Canary Monitor Execution Finished Successfully.")
