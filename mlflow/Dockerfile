FROM ghcr.io/mlflow/mlflow:v3.1.1

# boto3/psycopg2: S3 artifacts + PostgreSQL backend for the tracking server.
# scikit-learn + uvicorn: this image doubles as the model-serving runtime
# (`mlflow models serve --env-manager local` via the serving manager), so it
# must be able to load and score the models the platform trains.
# scikit-learn is pinned to match python/pipelines/Dockerfile exactly:
# trained pickles must load in this image.
RUN pip install --no-cache-dir \
    boto3==1.38.46 \
    psycopg2-binary==2.9.10 \
    scikit-learn==1.7.0 \
    uvicorn==0.34.3
