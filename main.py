FROM python:3.11

WORKDIR /app

# نسخ كل المحتويات إلى المجلد الحالي في السيرفر
COPY . .

# تثبيت المكتبات (نستخدم المسار المباشر لأننا صرنا داخل المجلد)
RUN pip install --no-cache-dir -r requirements.txt

# تشغيل الملف الرئيسي
CMD ["python", "main.py"]
