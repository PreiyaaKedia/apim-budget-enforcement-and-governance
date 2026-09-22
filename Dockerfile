FROM mcr.microsoft.com/azurelinux/base/python:3.12.14-1-azl3.0.20260909

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY main.py .

EXPOSE 8000
CMD ["python3", "main.py"]