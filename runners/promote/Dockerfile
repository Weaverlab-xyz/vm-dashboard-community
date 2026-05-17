# Promote-runner image — transient task that converts and uploads a VM
# disk image between cloud storage backends as part of cross-cloud
# image promotion. Launched as an ECS Fargate task / Azure ACI container
# / GCP Cloud Run job by the dashboard. Stays slim so cold-start is
# fast and the runtime footprint is bounded.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         qemu-utils \
         ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Cloud SDKs cover all three targets so a single image works for AWS / Azure / GCP.
RUN pip install --no-cache-dir \
        "boto3>=1.34" \
        "azure-identity>=1.15" \
        "azure-storage-blob>=12.19" \
        "google-cloud-storage>=2.14"

COPY entrypoint.py /entrypoint.py
RUN chmod +x /entrypoint.py

ENTRYPOINT ["python", "/entrypoint.py"]
