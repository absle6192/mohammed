import websocket
import ssl
import json
import os

# بيانات الدخول (يفضل وضعها في Environment Variables بسيرفر Koyeb)
USER = os.getenv('RITHMIC_USER', 'nffn00@gmail.com')
PASS = os.getenv('RITHMIC_PASS', 'كلمة_المرور_الخاصة_بك')
SYSTEM = 'Rithmic Paper Trading' # نظام الحساب التجريبي

def on_message(ws, message):
    print(f"--- رسالة من Rithmic: {message}")

def on_error(ws, error):
    print(f"--- خطأ في الاتصال: {error}")

def on_close(ws, close_status_code, close_msg):
    print("--- تم إغلاق الاتصال بالسيرفر ---")

def on_open(ws):
    print("--- جاري محاولة تسجيل الدخول... ---")
    # هذا هو "طلب المصافحة" لإثبات هويتك للسيرفر
    auth_request = {
        "user": USER,
        "password": PASS,
        "system": SYSTEM,
        "app_id": "DEMA", # معرف تجريبي للمبرمجين
        "version": "1.0"
    }
    ws.send(json.dumps(auth_request))

if __name__ == "__main__":
    # رابط سيرفر Rithmic للـ API (تجريبي)
    # ملاحظة: العناوين الفعلية قد تختلف حسب المنطقة، سنبدأ بهذا العنوان العام:
    uri = "wss://paper-trading.rithmic.com:443" 

    ws = websocket.WebSocketApp(uri,
                              on_open=on_open,
                              on_message=on_message,
                              on_error=on_error,
                              on_close=on_close)

    # تشغيل الاتصال مع تخطي فحص الـ SSL إذا لزم الأمر للتجربة
    ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
