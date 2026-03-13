import requests
import time
import sys

# البيانات المؤكدة من صورك الأخيرة
TG_TOKEN = "8057957727:AAF970v5y3RCGT7WsssqoCMEdDE7qjxDNwo"
TG_CHAT_ID = "1682557412"

def send_final():
    # استخدام رابط مباشر مع مكتبة requests بشكل مبسط جداً
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {
        "chat_id": TG_CHAT_ID,
        "text": "🛡️ المحاولة الأخيرة: الربط شغال والسيرفر الأمريكي استجاب يا إياد!"
    }
    
    try:
        # إرسال الطلب مع مهلة زمنية 15 ثانية
        response = requests.post(url, data=data, timeout=15)
        # طباعة النتيجة فوراً في سجلات Koyeb لتعرف ماذا حدث
        print(f"--- RESULT ---")
        print(f"Status Code: {response.status_code}")
        print(f"Response Body: {response.text}")
        print(f"--------------")
        return response.status_code == 200
    except Exception as e:
        print(f"⚠️ Error: {str(e)}")
        return False

if __name__ == "__main__":
    print("🚀 بدء تشغيل البوت...")
    success = send_final()
    
    if success:
        print("✅ تم إرسال الرسالة بنجاح!")
    else:
        print("❌ فشل الإرسال، راجع السجلات أعلاه.")
    
    # حلقة بسيطة ليبقى السيرفر Healthy ولا يتوقف
    while True:
        time.sleep(60)
