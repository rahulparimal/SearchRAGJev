FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt constraints.txt ./
# CPU-only torch first (the default Linux wheel pulls multi-GB CUDA libraries); wheels exist for amd64 and arm64.
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu torch==2.2.2 -c constraints.txt \
 && pip install --no-cache-dir -r requirements.txt -c constraints.txt
# Bake the embedding model into the image: the first query never downloads anything and the startup warm-up stays short.
ENV HF_HOME=/opt/models
ARG EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
RUN python -c "import os;from sentence_transformers import SentenceTransformer as S;S(os.environ['EMBEDDING_MODEL'])"
COPY *.py .
# scripts/ (ingest_samples.py for `./rag seed`) lives outside this build context (./backend): docker-compose.yml mounts ./scripts read-only at /app/scripts.
RUN mkdir -p /app/uploads
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
