FROM nvcr.io/nvidia/pytorch:23.10-py3

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git

#RUN git clone https://github.com/vllm-project/vllm.git && \
#    cd vllm && \
#    pip install -e . 

COPY requirements.txt /workspace/
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY run_vllm.sh /workspace/

ENTRYPOINT [ "/workspace/run_vllm.sh" ]