FROM python:3.11

WORKDIR /app

# نسخ كل الملفات (اللي هي أصلاً صارت منظورة للبوت)
COPY . .

# تثبيت المكتبات (بدون كتابة اسم المجلد)
RUN pip install --no-cache-dir -r requirements.txt

# تشغيل البوت مباشرة
CMD ["python", "main.py"]
