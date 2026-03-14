FROM python:3.11
WORKDIR /app
# هذي النقطة هي التغيير (نسخ كل شي داخل المجلد)
COPY . .
RUN pip install --no-cache-dir -r mohammed/requirements.txt
# هنا المسار الجديد للملف
CMD ["python", "mohammed/main.py"]
