# Deployment Process
Services deploy through the pipeline: build, test, staging, canary at five percent, then full rollout. Production deploys are frozen on Fridays after 14:00. A failed canary rolls back automatically.
