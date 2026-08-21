from app.llm import LocalLLM

def test_json_extraction():
    assert LocalLLM._json_object('```json\n{"value":"Josh","confidence":0.9}\n```')["value"] == "Josh"
    assert LocalLLM._json_object("not json") == {}
