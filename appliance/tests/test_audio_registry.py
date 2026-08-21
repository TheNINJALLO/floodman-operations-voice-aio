import numpy as np
from app.audio import rms, chunk_pcm
from app.registry import CallRegistry

def test_audio_helpers():
    tone=(np.ones(800,dtype='<i2')*1000).tobytes()
    assert rms(tone)==1000
    assert len(list(chunk_pcm(tone,8000,20)))==5

def test_registry(tmp_path):
    registry=CallRegistry(tmp_path);registry.write_action("abc","transfer","+12315550000","test")
    assert (tmp_path/"actions/abc.json").exists()
