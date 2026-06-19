import os
import json
import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone, timedelta

# ==========================================
# CanaryInTheGrid v2.0 - Guerrilla + Sheets Logging Edition
# ==========================================

def fetch_cme_natural_gas_data():
    print("[*] Infiltrating CME Group for Natural Gas Futures...")
    cme_url = "https://www.cmegroup.com/CmeWS/mvc/Quotes/Future/425/G"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.cmegroup.com/"
    }
    try:
        response = requests.get(cme_url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        if len(data) > 24:
            far_month_data = data[24]
            price = float(far_month_data.get('last', 0)) or float(far_month_data.get('priorSettle', 0))
            volume = int(far_month_data.get('volume', 0).replace(',', '')) if isinstance(far_month_data.get('volume'), str) else far_month_data.get('volume', 0)
            print(f"[+] CME Data Fetched: Price={price}, Volume={volume}")
            return {"price": price, "volume": volume}
        else:
            raise Exception("Not enough forward curve data available.")
    except Exception as e:
        print(f"[-] CME Fetch Failed: {e}")
        return {"price": 5.0, "volume": 1000} 

def fetch_eia_storage_data(api_key):
    print("[*] Fetching Macro Inventory Data from US EIA...")
    eia_url = f"https://api.eia.gov/v2/natural-gas/stor/sum/data/?api_key={api_key}&frequency=weekly&data[0]=value&sort[0][column]=period&sort[0][direction]=desc&offset=0&length=1"
    try:
        response = requests.get(eia_url, timeout=10)
        response.raise_for_status()
        data = response.json()
        latest_storage = data['response']['data'][0]['value']
        print(f"[+] EIA Data Fetched: {latest_storage} Bcf")
        return latest_storage
    except Exception as e:
        print(f"[-] EIA API Fetch Failed: {e}")
        return None

def evaluate_matrix(cme_data, eia_storage):
    print("[*] Evaluating System Matrix (Guerrilla Logic)...")
    gas_price_collapsed = cme_data["price"] < 2.5
    volume_dropped = cme_data["volume"] < 500
    inventory_oversupplied = eia_storage is not None and eia_storage > 3500 

    if gas_price_collapsed and volume_dropped:
        state = "🔴 【崩壊確定】フル・ショート準備"
        desc = "ベース燃料の価格崩壊と流動性消失を確認。マクロ崩壊が進行中。"
    elif not gas_price_collapsed and volume_dropped:
        state = "🟠 【真空状態】早期警戒シグナル"
        desc = "価格は維持されているが、買い手が消失。暴落前夜の可能性。"
    elif gas_price_collapsed and not volume_dropped:
        state = "🟡 【偽陽性】ノイズ検知"
        desc = "価格に異常値が出たが、流動性の裏付けなし。静観。"
    else:
        state = "🟢 【正常】バブル継続中"
        desc = "燃料市場は正常に機能中。カナリアは健康だ。"

    if state != "🟢 【正常】バブル継続中" and inventory_oversupplied:
        desc += "\n🚨 **Phase 3 認証完了**: EIA在庫の異常なダブつきを確認。シグナルの信頼性は極めて高い。"

    return {
        "state": state,
        "desc": desc,
        "gas_price": cme_data["price"],
        "gas_volume": cme_data["volume"],
        "eia_storage": eia_storage,
        "phase1_fired": gas_price_collapsed,
        "phase2_fired": volume_dropped
    }

def send_ntfy_alert(result, topic):
    print("[*] Sending Alert via ntfy...")
    url = f"https://ntfy.sh/{topic}"
    
    if "🔴" in result['state']:
        tags = "rotating_light,skull"
        priority = "urgent"
    elif "🟠" in result['state']:
        tags = "warning,chart_with_downwards_trend"
        priority = "high"
    elif "🟡" in result['state']:
        tags = "eyes"
        priority = "default"
    else:
        tags = "green_circle,bird"
        priority = "low"
        
    headers = {
        "Title": "CanaryInTheGrid v2.0",
        "Priority": priority,
        "Tags": tags,
        "Markdown": "yes"
    }
    
    message = f"**ステータス:** {result['state']}\n\n"
    message += f"{result['desc']}\n\n"
    message += f"📊 **天然ガス遠月物価格:** ${result['gas_price']} / MMBtu\n"
    message += f"📉 **遠月物 出来高:** {result['gas_volume']} contracts\n"
    message += f"🛢️ **EIA 天然ガス在庫:** {result['eia_storage']} Bcf"
    
    response = requests.post(url, data=message.encode('utf-8'), headers=headers)
    
    if response.status_code == 200:
        print("[+] ntfy Alert sent successfully.")
    else:
        print(f"[-] Failed to send ntfy alert. Status: {response.text}")

def log_to_google_sheets(result, credentials_json_str, sheet_id):
    """
    検証用: 取得データと判定結果をGoogle Spreadsheetに追記する
    """
    print("[*] Appending log to Google Sheets...")
    try:
        # JSON文字列を辞書オブジェクトに変換して認証
        credentials_info = json.loads(credentials_json_str)
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        credentials = Credentials.from_service_account_info(credentials_info, scopes=scopes)
        gc = gspread.authorize(credentials)
        
        # スプレッドシートを開き、最初のシート（Sheet1）を選択
        workbook = gc.open_by_key(sheet_id)
        worksheet = workbook.sheet1
        
        # 記録するデータの準備（日本時間）
        jst_time = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M:%S')
        
        # ヘッダー順に合わせたリストを作成
        row_data = [
            jst_time,
            result['state'],
            result['gas_price'],
            result['gas_volume'],
            result['eia_storage'] if result['eia_storage'] is not None else "N/A",
            "発火 (異常)" if result['phase1_fired'] else "正常",
            "発火 (異常)" if result['phase2_fired'] else "正常"
        ]
        
        # 行の追記 (最下部に自動で1行追加される)
        worksheet.append_row(row_data)
        print("[+] Data successfully logged to Google Sheets.")
        
    except Exception as e:
        print(f"[-] Failed to log data to Google Sheets: {e}")

if __name__ == "__main__":
    NTFY_TOPIC = os.getenv("NTFY_TOPIC")
    EIA_API_KEY = os.getenv("EIA_API_KEY")
    GCP_SHEET_ID = os.getenv("GCP_SHEET_ID")
    GCP_SERVICE_ACCOUNT_JSON = os.getenv("GCP_SERVICE_ACCOUNT_JSON")
    
    if not NTFY_TOPIC or not EIA_API_KEY or not GCP_SHEET_ID or not GCP_SERVICE_ACCOUNT_JSON:
        print("[-] Error: Missing one or more Environment Variables.")
        exit(1)

    cme_data = fetch_cme_natural_gas_data()
    eia_storage = fetch_eia_storage_data(EIA_API_KEY)
    eval_result = evaluate_matrix(cme_data, eia_storage)
    
    send_ntfy_alert(eval_result, NTFY_TOPIC)
    log_to_google_sheets(eval_result, GCP_SERVICE_ACCOUNT_JSON, GCP_SHEET_ID)
