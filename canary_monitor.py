import os
import requests
from datetime import datetime, timezone, timedelta

# ==========================================
# CanaryInTheGrid v2.0 - Guerrilla & ntfy Edition
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
        "eia_storage": eia_storage
    }

def send_ntfy_alert(result, topic):
    print("[*] Sending Alert via ntfy...")
    url = f"https://ntfy.sh/{topic}"
    
    # 状態に応じてスマホの通知優先度（鳴り方）とアイコンを変更する
    if "🔴" in result['state']:
        tags = "rotating_light,skull"
        priority = "urgent" # 最高優先度（マナーモードでも鳴る設定が可能）
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
    
    # Markdown形式でメッセージ本文を組み立てる
    message = f"**ステータス:** {result['state']}\n\n"
    message += f"{result['desc']}\n\n"
    message += f"📊 **天然ガス遠月物価格:** ${result['gas_price']} / MMBtu\n"
    message += f"📉 **遠月物 出来高:** {result['gas_volume']} contracts\n"
    message += f"🛢️ **EIA 天然ガス在庫:** {result['eia_storage']} Bcf"
    
    # ntfyサーバーへPOSTリクエスト（UTF-8エンコード必須）
    response = requests.post(url, data=message.encode('utf-8'), headers=headers)
    
    if response.status_code == 200:
        print("[+] ntfy Alert sent successfully.")
    else:
        print(f"[-] Failed to send ntfy alert. Status: {response.text}")

if __name__ == "__main__":
    NTFY_TOPIC = os.getenv("NTFY_TOPIC")
    EIA_API_KEY = os.getenv("EIA_API_KEY")
    
    if not NTFY_TOPIC or not EIA_API_KEY:
        print("[-] Error: Missing Environment Variables (NTFY_TOPIC or EIA_API_KEY).")
        exit(1)

    cme_data = fetch_cme_natural_gas_data()
    eia_storage = fetch_eia_storage_data(EIA_API_KEY)
    
    eval_result = evaluate_matrix(cme_data, eia_storage)
    send_ntfy_alert(eval_result, NTFY_TOPIC)