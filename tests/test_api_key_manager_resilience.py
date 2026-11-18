import os
import json
import pytest
from src.api_key_manager import APIKeyManager
from cryptography.fernet import Fernet

def test_api_key_manager_load_resilience(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    mgr.mark_installation_state("first_run_pending")
    mgr.initialize_key(first_time=True)
    
    # Save a valid key
    mgr.save("good_provider", "good_value")
    
    # Create another key manager/Fernet instance with a different key to produce an undecryptable token
    other_key = Fernet.generate_key()
    other_f = Fernet(other_key)
    undecryptable_token = other_f.encrypt(b"bad_value").decode()
    
    # Manually edit api_keys.json to include the undecryptable token
    with open(mgr.api_keys_file, "r", encoding="utf-8") as f:
        keys = json.load(f)
    
    keys["bad_provider"] = undecryptable_token
    # Also add a malformed/garbage token (causes ValueError/binascii.Error)
    keys["garbage_provider"] = "not-a-valid-base64-fernet-token"
    
    with open(mgr.api_keys_file, "w", encoding="utf-8") as f:
        json.dump(keys, f)
        
    # Corrupted or wrong-key ciphertext must fail closed rather than silently
    # dropping providers or appearing to delete them.
    with pytest.raises(Exception, match="could not be authenticated"):
        mgr.load()


def test_load_ignores_non_string_raw_values(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    mgr.mark_installation_state("first_run_pending")
    mgr.initialize_key(first_time=True)

    mgr.save("openai", "sk-openai")
    with open(mgr.api_keys_file, "r", encoding="utf-8") as f:
        keys = json.load(f)

    keys["missing_provider"] = None
    keys["numeric_provider"] = 42
    keys["object_provider"] = {"encrypted": keys["openai"]}
    with open(mgr.api_keys_file, "w", encoding="utf-8") as f:
        json.dump(keys, f)

    assert mgr.load() == {"openai": "sk-openai"}
