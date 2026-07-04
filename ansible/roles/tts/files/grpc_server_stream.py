# Патченный runtime/python/grpc/server.py из FastCosyVoice (Apache-2.0,
# (c) 2024 Alibaba Inc). Отличия от оригинала:
#   1. --stream: inference_* вызываются со stream=True — аудио отдаётся чанками
#      по мере генерации, а не предложением целиком (критично для TTFB телефонии);
#   2. prompt_audio (raw PCM s16le 16k) пишется во временный WAV в /dev/shm:
#      CLI этого форка ожидает файл/путь (оригинальный серверный код передавал
#      тензор и падал), причём frontend читает файл ТРИЖДЫ (feat, token,
#      embedding) — BytesIO не годится, нужен переоткрываемый путь;
#   3. логирование INFO вместо DEBUG.
# Файл монтируется в контейнер поверх runtime/python/grpc/server.py
# (ansible/roles/tts/templates/docker-compose.yml.j2).
import os
import sys
import tempfile
import wave
from concurrent import futures
import argparse
import cosyvoice_pb2
import cosyvoice_pb2_grpc
import logging
logging.getLogger('matplotlib').setLevel(logging.WARNING)
import grpc
import torch
import numpy as np
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append('{}/../../..'.format(ROOT_DIR))
sys.path.append('{}/../../../third_party/Matcha-TTS'.format(ROOT_DIR))
from cosyvoice.cli.cosyvoice import CosyVoice, CosyVoice2

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')

PROMPT_SR = 16000
PROMPT_TMP_DIR = '/dev/shm' if os.path.isdir('/dev/shm') else None


def pcm16_to_tmp_wav(pcm: bytes) -> str:
    """raw PCM s16le 16k mono -> временный WAV-файл, возвращает путь.

    frontend читает prompt_wav несколько раз (feat/token/embedding),
    поэтому нужен переоткрываемый путь, а не поток.
    """
    f = tempfile.NamedTemporaryFile(suffix='.wav', dir=PROMPT_TMP_DIR, delete=False)
    with wave.open(f, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(PROMPT_SR)
        wf.writeframes(pcm)
    f.close()
    return f.name


class CosyVoiceServiceImpl(cosyvoice_pb2_grpc.CosyVoiceServicer):
    def __init__(self, args):
        self.stream = args.stream
        # fp16 + TensorRT критичны: сток (fp32, python) даёт RTF > 1 даже
        # на A100 — синтез медленнее реального времени
        kwargs = dict(fp16=args.fp16, load_trt=args.load_trt,
                      trt_concurrent=args.max_conc)
        try:
            self.cosyvoice = CosyVoice(args.model_dir, **kwargs)
        except Exception:
            try:
                self.cosyvoice = CosyVoice2(args.model_dir, **kwargs)
            except Exception:
                logging.exception('model init failed')
                raise TypeError('no valid model_type!')
        logging.info('grpc service initialized, stream=%s fp16=%s trt=%s',
                     self.stream, args.fp16, args.load_trt)

    def Inference(self, request, context):
        prompt_path = None
        try:
            if request.HasField('sft_request'):
                model_output = self.cosyvoice.inference_sft(
                    request.sft_request.tts_text, request.sft_request.spk_id,
                    stream=self.stream)
            elif request.HasField('zero_shot_request'):
                prompt_path = pcm16_to_tmp_wav(request.zero_shot_request.prompt_audio)
                model_output = self.cosyvoice.inference_zero_shot(
                    request.zero_shot_request.tts_text,
                    request.zero_shot_request.prompt_text,
                    prompt_path, stream=self.stream)
            elif request.HasField('cross_lingual_request'):
                prompt_path = pcm16_to_tmp_wav(request.cross_lingual_request.prompt_audio)
                model_output = self.cosyvoice.inference_cross_lingual(
                    request.cross_lingual_request.tts_text, prompt_path,
                    stream=self.stream)
            else:
                model_output = self.cosyvoice.inference_instruct(
                    request.instruct_request.tts_text,
                    request.instruct_request.spk_id,
                    request.instruct_request.instruct_text, stream=self.stream)

            # генератор ленивый: frontend читает prompt-файл во время итерации,
            # поэтому удаляем файл только в finally, после конца стрима.
            # is_active: без этой проверки сервер догенерирует отменённые клиентом
            # запросы до конца («зомби» жгут CPU/GPU после конца полки)
            for i in model_output:
                if not context.is_active():
                    break
                response = cosyvoice_pb2.Response()
                response.tts_audio = (i['tts_speech'].numpy() * (2 ** 15)).astype(np.int16).tobytes()
                yield response
        finally:
            if prompt_path:
                try:
                    os.unlink(prompt_path)
                except OSError:
                    pass


def main():
    grpcServer = grpc.server(futures.ThreadPoolExecutor(max_workers=args.max_conc),
                             maximum_concurrent_rpcs=args.max_conc)
    cosyvoice_pb2_grpc.add_CosyVoiceServicer_to_server(CosyVoiceServiceImpl(args), grpcServer)
    grpcServer.add_insecure_port('0.0.0.0:{}'.format(args.port))
    grpcServer.start()
    logging.info("server listening on 0.0.0.0:%s", args.port)
    grpcServer.wait_for_termination()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=50000)
    parser.add_argument('--max_conc', type=int, default=4,
                        help='max_workers и лимит одновременных RPC: сверх лимита '
                             'запросы отклоняются (RESOURCE_EXHAUSTED)')
    parser.add_argument('--stream', action='store_true',
                        help='отдавать аудио чанками по мере генерации')
    parser.add_argument('--fp16', action='store_true')
    parser.add_argument('--load_trt', action='store_true',
                        help='TensorRT для flow decoder (первый старт строит план, ~10-20 мин)')
    parser.add_argument('--model_dir', type=str, default='iic/CosyVoice-300M',
                        help='local path or modelscope repo id')
    args = parser.parse_args()
    main()
