FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt constraints.txt ./
# CPU-only torch first (the default Linux wheel pulls multi-GB CUDA libraries).
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu torch==2.2.2 -c constraints.txt \
 && pip install --no-cache-dir -r requirements.txt -c constraints.txt
COPY *.py .
RUN mkdir -p /app/uploads
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
