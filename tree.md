.
├── ACKNOWLEDGMENTS.md
├── app.py
├── assets
│   └── branding
│       ├── odysseus-browser.jpg
│       ├── odysseus.jpg
│       └── odysseus-wordmark.png
├── build-macos-app.sh
├── build-windows-portable.ps1
├── companion
│   ├── __init__.py
│   ├── pairing.py
│   ├── README.md
│   └── routes.py
├── config
│   └── searxng
│       └── settings.yml
├── CONTRIBUTING.md
├── core
│   ├── atomic_io.py
│   ├── auth.py
│   ├── constants.py
│   ├── database.py
│   ├── exceptions.py
│   ├── __init__.py
│   ├── log_safety.py
│   ├── middleware.py
│   ├── models.py
│   ├── platform_compat.py
│   ├── secure_files.py
│   └── session_manager.py
├── data
│   ├── agent_workspace
│   ├── app.db
│   ├── auth.json
│   ├── cache
│   │   ├── content
│   │   └── search
│   ├── chroma
│   ├── deep_research
│   ├── email_urgency_cache
│   ├── emoji_cache
│   │   └── 1f60a.svg
│   ├── fastembed_cache
│   │   ├── CACHEDIR.TAG
│   │   └── models--qdrant--all-MiniLM-L6-v2-onnx
│   ├── generated_images
│   ├── huggingface
│   │   └── xet
│   ├── hwfit
│   │   ├── hf_collection_models.json
│   │   └── mlx_community_models.json
│   ├── local
│   │   └── playwright-mcp-cache
│   ├── logs
│   │   ├── app.log
│   │   └── search_engine_error.log
│   ├── mail-attachments
│   │   └── _compose
│   ├── memory.json
│   ├── memory_vectors
│   ├── models
│   │   └── stt
│   ├── note_pings_admin.json
│   ├── personal_docs
│   │   └── runbook
│   ├── personal_uploads
│   ├── presets.json
│   ├── rag
│   ├── scheduled_emails.db
│   ├── sessions.json
│   ├── settings.json
│   ├── skills
│   ├── ssh
│   ├── tts_cache
│   │   ├── 05920b256d8f5827e3b430c354ca16d19475ea8079c81a0bb5412451759f7adb.wav
│   │   ├── 0e533c4f6a5d35fd43cd082d27be4b3af4f3e880641c61d3af9613711a880abc.wav
│   │   ├── 16447cde0f9e8099d97a5283a5bfda94095c9a8c81679a867256d8fa5b6ea362.wav
│   │   ├── 164b16cafb2c7d3802b782d153a85f2fce36bb9bdd4806ff57ce687d5c5f9501.wav
│   │   ├── 18f568f51876c24d958bdb80eabf29781d8ccc7e3f0cadb3d5ce7bf5c2f54e62.wav
│   │   ├── 1f727d402d49140acbc0f8a368e53affa23c1ec13cc15bdea8869cbadd6039d2.wav
│   │   ├── 2a3fd315e3d980d86d025f3468d50d6bb6c958428fe10160280abe11ce0bb3e6.wav
│   │   ├── 2ba09e180d13667acca5baed26aeacea580f683434e8624c87906fe3653e1588.wav
│   │   ├── 2ccdf812c78520174adbe283bf8c8af3e3f456c06bd6ab0bbb0e31c40e89b861.wav
│   │   ├── 2d573e2d848baf2d015e1e2e9d92b7f9bb9a664693365e26f4bae9659f4ef79a.wav
│   │   ├── 2f4a7b8b5d216fa078c1bad0a06c004e31f2e7aa99b8ef96690d8a010b4e0098.wav
│   │   ├── 34743b804d3df8ad7b43d18f11581a236705d63c53e685cb110b048f5926d767.wav
│   │   ├── 34db9e36b4a2ae86323f8ab83671cc789e263841491bdf5c4e6b8365895d3f9f.wav
│   │   ├── 3d8bda679b34bbf22533c1f8cc86638fdd67f60d75b304902fd31551c0177b99.wav
│   │   ├── 3e9bd91f4d679f3ea8f6b22a5ee4a241a34395ed0db25f9fc91e369ca992b43c.wav
│   │   ├── 3ed41894cbfb0d7422a392dcf7aec04abb0c97d177524ac5cd9b049057c7ea60.wav
│   │   ├── 4069e837cc2c0674779fb0a411b596edbe531f41ace9e1bd26d7875e68ec4cba.wav
│   │   ├── 44eba73d9b6389b426fdfa9f843edfc83ee35749bf9e17a50ac3c49929641663.wav
│   │   ├── 4e9c91d9db5190f0fa565bed4f503d9c8a6a8fd5fdebfb9cc39886971d7d0d90.wav
│   │   ├── 4f225ead42631171037c0bf6ec65c6d1c996c1df098de71412ef350361f3d2e7.wav
│   │   ├── 51565db7f12823a780e3a0e3a45dba2b14329769f439a522779b923608979142.wav
│   │   ├── 69fefcd0337116d7dbaa0b7053d7239cf0414c2168a6a5890a205b42d8cfa1ff.wav
│   │   ├── 6a9855432266edcedb7c74c5ba0b7cc41e8bf9070ba90a999c40dd4234ad59e3.wav
│   │   ├── 6f676939bc907c7ecd0b6c5a9bf82a43ffddfc84be14f662d4ed71c96099d772.wav
│   │   ├── 7288ee70b328a5f81b4c527009b52035462979a217040efa9cfa9275a56b5b75.wav
│   │   ├── 8d312f3460fc701fe62831ba385c35a6b313ffa9ccb58f5066c473246b00517c.wav
│   │   ├── a384eb14d9394f5a5d1168ab2cd6ead32406945c8e445c660cc6cda98650b782.wav
│   │   ├── a6f79b3e433f3069a9d80654c072c60b8d3bf60aef1482539ab1a60519a9ea4e.wav
│   │   ├── b2d3961b4064b30bca7803a34275056c2784d174c5d790012ecfbeb8d06b7ef5.wav
│   │   ├── b906af9eb3de86e8ca0e87a949c1fc4d262d0c6ce36b74fc5ef6c087f8dcc88b.wav
│   │   ├── bd8335fceb42f33fe88b027625101f8cf7d325eb31c1656f79beb98b584b447b.wav
│   │   ├── c2029a8f6e5ec95a9882121e81a5c70b40cd664529dba2d70f4d8ea7db0ce766.wav
│   │   ├── ca6e6026aad158aa5a4e48910e5a1cbc6e4db956b207e205de7059b6eeccaa8f.wav
│   │   ├── cf84db16f0844d738f5ecb22f63106f19f89c301d4111ff307e2bf621051c0a1.wav
│   │   ├── d11fe8cd41754b8a4e9fc957adc511dd95dffdec5fd09cef8c245cf54ae865f7.wav
│   │   ├── d1bac64510c2da2a00cc7c7aa29600693dcd14c42464dcf3621fa9dd50bbfefa.wav
│   │   ├── db99ba0c31046d85a65da51fddcd717561926a3bcd889b1d60e27ccdc7e4a006.wav
│   │   ├── df53a4976e4fcf4e7faf34d56075f4ee827d0e7fb3b4e1bdcab9d6a94fe89cbe.wav
│   │   ├── e585e6b06d741cf5ff312281f5b22c8fec9683a0aeda0ab0755b5fd9f261d15f.wav
│   │   ├── eedbaaacd70cb23c35d00580794f2e0a35bf88e2c2ebf60780ffdf3ed5b80bad.wav
│   │   ├── f26d8addd6fab60aa06947c3e6a4105c68c8d2376a3eea57bfbb59c88115ceb8.wav
│   │   ├── fb8edfd62e2b72f0bdf5e80b1d768753e074dbf6ff2c62f85309e8c655f3d4be.wav
│   │   └── ffe7b4ec6592717b744d888983bf46bcd93ad7001d8b5dd9a49bebd82a12d214.wav
│   └── uploads
├── desktop
│   ├── bin
│   │   └── odysseus-voice
│   ├── index.html
│   ├── package.json
│   ├── package-lock.json
│   ├── README.md
│   ├── src
│   │   ├── audio.js
│   │   ├── conversation.js
│   │   ├── main.js
│   │   ├── microphone.js
│   │   └── speech.js
│   ├── src-tauri
│   │   ├── build.rs
│   │   ├── capabilities
│   │   ├── Cargo.lock
│   │   ├── Cargo.toml
│   │   ├── gen
│   │   ├── icons
│   │   ├── src
│   │   ├── target
│   │   └── tauri.conf.json
│   ├── style.css
│   ├── tests
│   │   ├── audio.test.js
│   │   ├── conversation.test.js
│   │   ├── microphone.test.js
│   │   └── speech.test.js
│   └── vite.config.js
├── docker
│   ├── build-realesrgan-wheels.sh
│   ├── entrypoint.sh
│   ├── gpu.amd.yml
│   ├── gpu.nvidia.yml
│   └── host-docker.yml
├── docker-compose.gpu-amd.yml
├── docker-compose.gpu-nvidia.yml
├── docker-compose.yml
├── Dockerfile
├── docs
│   └── ARCHITECTURE_OVERVIEW.md
├── GEMINI.md
├── host_bridge
│   ├── actions.py
│   ├── config.py
│   ├── hotkey.py
│   ├── __init__.py
│   ├── README.md
│   └── server.py
├── install-host-bridge-user-service.sh
├── install-service.sh
├── integrations
│   ├── claude
│   │   ├── README.md
│   │   └── skills
│   └── codex
│       ├── README.md
│       ├── scripts
│       └── skills
├── launcher.py
├── launch-windows.ps1
├── LICENSE
├── licenses
│   ├── DeepResearch-Apache-2.0.txt
│   ├── KaTeX-MIT-LICENSE.txt
│   ├── llmfit-MIT-LICENSE.txt
│   ├── Mermaid-MIT-LICENSE.txt
│   ├── opencode-MIT-LICENSE.txt
│   └── OpenDyslexic-OFL.txt
├── logs
├── mcp_servers
│   ├── email_server.py
│   ├── image_gen_server.py
│   ├── __init__.py
│   ├── memory_server.py
│   └── rag_server.py
├── odysseus-host-bridge.service
├── Odysseus.spec
├── odysseus-ui.service
├── ollama-models
│   ├── blobs
│   └── manifests
├── package.json
├── package-lock.json
├── pyproject.toml
├── README.md
├── requirements-optional.txt
├── requirements-stt.txt
├── requirements.txt
├── ROADMAP.md
├── routes
│   ├── admin_wipe
│   │   ├── admin_wipe_routes.py
│   │   └── __init__.py
│   ├── admin_wipe_routes.py
│   ├── api_token_routes.py
│   ├── assistant_routes.py
│   ├── auth_routes.py
│   ├── backup_routes.py
│   ├── calendar_routes.py
│   ├── chatgpt_subscription_routes.py
│   ├── chat_helpers.py
│   ├── chat_routes.py
│   ├── cleanup
│   │   ├── cleanup_routes.py
│   │   └── __init__.py
│   ├── cleanup_routes.py
│   ├── codex_routes.py
│   ├── compare
│   │   ├── compare_routes.py
│   │   └── __init__.py
│   ├── compare_routes.py
│   ├── contacts
│   │   ├── contacts_routes.py
│   │   └── __init__.py
│   ├── contacts_routes.py
│   ├── cookbook_helpers.py
│   ├── cookbook_output.py
│   ├── cookbook_routes.py
│   ├── copilot_routes.py
│   ├── device_flow.py
│   ├── diagnostics_routes.py
│   ├── document
│   │   ├── document_helpers.py
│   │   ├── document_routes.py
│   │   └── __init__.py
│   ├── document_helpers.py
│   ├── document_routes.py
│   ├── editor_draft_routes.py
│   ├── email_helpers.py
│   ├── email_pollers.py
│   ├── email_routes.py
│   ├── embedding_routes.py
│   ├── emoji_routes.py
│   ├── font_routes.py
│   ├── gallery
│   │   ├── gallery_helpers.py
│   │   ├── gallery_routes.py
│   │   └── __init__.py
│   ├── gallery_helpers.py
│   ├── gallery_routes.py
│   ├── history
│   │   ├── history_routes.py
│   │   └── __init__.py
│   ├── history_routes.py
│   ├── hwfit_routes.py
│   ├── __init__.py
│   ├── mcp
│   │   ├── __init__.py
│   │   └── mcp_routes.py
│   ├── mcp_routes.py
│   ├── memory
│   │   ├── __init__.py
│   │   └── memory_routes.py
│   ├── memory_routes.py
│   ├── model_routes.py
│   ├── note
│   │   ├── __init__.py
│   │   └── note_routes.py
│   ├── note_routes.py
│   ├── personal_routes.py
│   ├── prefs_routes.py
│   ├── preset_routes.py
│   ├── research
│   │   ├── __init__.py
│   │   └── research_routes.py
│   ├── research_routes.py
│   ├── search
│   │   ├── __init__.py
│   │   └── search_routes.py
│   ├── search_routes.py
│   ├── session_routes.py
│   ├── shell_routes.py
│   ├── signature_routes.py
│   ├── skills_routes.py
│   ├── stt_routes.py
│   ├── task
│   │   ├── __init__.py
│   │   └── task_routes.py
│   ├── task_routes.py
│   ├── tts_routes.py
│   ├── upload_routes.py
│   ├── _validators.py
│   ├── vault
│   │   ├── __init__.py
│   │   └── vault_routes.py
│   ├── vault_routes.py
│   ├── webhook
│   │   ├── __init__.py
│   │   └── webhook_routes.py
│   ├── webhook_routes.py
│   └── workspace_routes.py
├── run_host_bridge.py
├── scratch
│   ├── apply_lint_fixes.py
│   ├── benchmark_model_lifecycle.py
│   ├── benchmark_results.json
│   ├── captured_desktop.jpg
│   ├── capture_test.py
│   ├── jarvis_vision_evaluation.py
│   ├── live_closed_loop_test.py
│   ├── live_gui_test.py
│   ├── live_host_bridge_test.py
│   ├── run_manual_benchmark.py
│   ├── test_bridge_listener.py
│   ├── test_computer_control_live.py
│   ├── test_diagnostics.py
│   ├── test_pipeline_prototype.py
│   ├── test_planner_speed.py
│   ├── test_qwen_computer_control.py
│   ├── test_qwen_gui_control.py
│   ├── test_screenshot.py
│   ├── test_tool_call_speed.py
│   ├── test_unbiased_vision.py
│   ├── test_vision_speed.py
│   ├── verify_single_instance.py
│   └── vision_evaluation_results.json
├── scripts
│   ├── add_hwfit_models.py
│   ├── agent_migration_manifest.py
│   ├── backfill_model_release_dates.py
│   ├── check-docker-amd-gpu.sh
│   ├── check-docker-gpu.sh
│   ├── claim_ownerless.py
│   ├── _completion
│   │   ├── odysseus.bash
│   │   └── odysseus.zsh
│   ├── demo_email
│   │   ├── demo_account.py
│   │   ├── manage.sh
│   │   └── seed_demo_emails.py
│   ├── diffusion_server.py
│   ├── docker
│   │   ├── check-docker-amd-gpu.sh
│   │   └── check-docker-gpu.sh
│   ├── encode_previews.sh
│   ├── fix_paths.py
│   ├── hf_download.py
│   ├── import_from_vllm_recipes.py
│   ├── index_documents.py
│   ├── _lib
│   │   ├── cli.py
│   │   └── __init__.py
│   ├── migrate_faiss_to_chroma.py
│   ├── migrate_searxng_settings.py
│   ├── mlx_image_server.py
│   ├── odysseus
│   ├── odysseus-backup
│   ├── odysseus-calendar
│   ├── odysseus-contacts
│   ├── odysseus-cookbook
│   ├── odysseus-docs
│   ├── odysseus-gallery
│   ├── odysseus-logs
│   ├── odysseus-mail
│   ├── odysseus-mcp
│   ├── odysseus-memory
│   ├── odysseus-notes
│   ├── odysseus-personal
│   ├── odysseus-preset
│   ├── odysseus-research
│   ├── odysseus-sessions
│   ├── odysseus-signature
│   ├── odysseus-skills
│   ├── odysseus-tasks
│   ├── odysseus-theme
│   ├── odysseus-webhook
│   ├── pr_blocker_audit.py
│   └── update_database.py
├── SECURITY.md
├── services
│   ├── docs
│   │   ├── __init__.py
│   │   └── service.py
│   ├── faces
│   │   └── __init__.py
│   ├── hwfit
│   │   ├── data
│   │   ├── fit.py
│   │   ├── hardware.py
│   │   ├── hf_discovery.py
│   │   ├── image_models.py
│   │   ├── __init__.py
│   │   ├── models.py
│   │   └── profiles.py
│   ├── __init__.py
│   ├── memory
│   │   ├── __init__.py
│   │   ├── memory_extractor.py
│   │   ├── memory.py
│   │   ├── memory_vector.py
│   │   ├── service.py
│   │   ├── skill_extractor.py
│   │   ├── skill_format.py
│   │   ├── skill_importer.py
│   │   └── skills.py
│   ├── notification
│   │   ├── __init__.py
│   │   └── ntfy.py
│   ├── research
│   │   ├── __init__.py
│   │   ├── research_handler.py
│   │   └── service.py
│   ├── search
│   │   ├── analytics.py
│   │   ├── cache.py
│   │   ├── content.py
│   │   ├── core.py
│   │   ├── __init__.py
│   │   ├── providers.py
│   │   ├── query.py
│   │   ├── ranking.py
│   │   └── service.py
│   ├── shell
│   │   ├── __init__.py
│   │   └── service.py
│   ├── stt
│   │   ├── __init__.py
│   │   └── stt_service.py
│   ├── tts
│   │   ├── __init__.py
│   │   └── tts_service.py
│   └── youtube
│       ├── __init__.py
│       └── youtube_handler.py
├── setup.py
├── specs
│   ├── agent-tools.md
│   ├── auth-security.md
│   ├── calendar-tasks-notes.md
│   ├── chat.md
│   ├── compare.md
│   ├── context-building.md
│   ├── cookbook-hwfit.md
│   ├── documents-rag-uploads.md
│   ├── email-contacts.md
│   ├── frontend.md
│   ├── gallery-editor-media.md
│   ├── integrations.md
│   ├── llm-models.md
│   ├── memory-skills.md
│   ├── model-capability-canonical.md
│   ├── model-providers
│   │   ├── anthropic.md
│   │   ├── atlas-cloud.md
│   │   ├── azure-openai.md
│   │   ├── bedrock.md
│   │   ├── cerebras.md
│   │   ├── chatgpt-subscription.md
│   │   ├── cloudflare-workers-ai.md
│   │   ├── cohere.md
│   │   ├── deepseek.md
│   │   ├── fireworks.md
│   │   ├── github-copilot.md
│   │   ├── github-models.md
│   │   ├── google.md
│   │   ├── groq.md
│   │   ├── hugging-face.md
│   │   ├── llama-cpp.md
│   │   ├── lm-studio.md
│   │   ├── local-compatible-engines.md
│   │   ├── minimax.md
│   │   ├── mistral.md
│   │   ├── moonshot-kimi.md
│   │   ├── nvidia-nim.md
│   │   ├── ollama.md
│   │   ├── openai-compatible.md
│   │   ├── openai.md
│   │   ├── opencode.md
│   │   ├── openrouter.md
│   │   ├── perplexity.md
│   │   ├── _readme.md
│   │   ├── sglang.md
│   │   ├── siliconflow.md
│   │   ├── together.md
│   │   ├── venice.md
│   │   ├── vllm.md
│   │   ├── xai.md
│   │   └── zai.md
│   ├── model-quirks.md
│   ├── persistence.md
│   ├── _readme.md
│   ├── research.md
│   ├── runtime.md
│   ├── search.md
│   ├── settings-admin.md
│   ├── shell-mcp.md
│   ├── speech.md
│   └── testing-devops.md
├── src
│   ├── action_intents.py
│   ├── agent_loop.py
│   ├── agent_runs.py
│   ├── agent_tools
│   │   ├── admin_tools.py
│   │   ├── bg_job_tools.py
│   │   ├── coding_tools.py
│   │   ├── computer_control_tools.py
│   │   ├── document_tools.py
│   │   ├── filesystem_tools.py
│   │   ├── __init__.py
│   │   ├── interaction_tools.py
│   │   ├── model_interaction_tools.py
│   │   ├── notification_tools.py
│   │   ├── session_tools.py
│   │   ├── subprocess_tools.py
│   │   └── web_tools.py
│   ├── ai_interaction.py
│   ├── api_key_manager.py
│   ├── app_helpers.py
│   ├── app_initializer.py
│   ├── assistant_log.py
│   ├── attachment_refs.py
│   ├── auth_helpers.py
│   ├── bg_jobs.py
│   ├── bg_monitor.py
│   ├── builtin_actions.py
│   ├── builtin_mcp.py
│   ├── caldav_sync.py
│   ├── caldav_writeback.py
│   ├── chatgpt_subscription.py
│   ├── chat_handler.py
│   ├── chat_helpers.py
│   ├── chat_processor.py
│   ├── chroma_client.py
│   ├── cleanup_service.py
│   ├── computer_use_controller.py
│   ├── config.py
│   ├── constants.py
│   ├── context_budget.py
│   ├── context_compactor.py
│   ├── cookbook_serve_lifecycle.py
│   ├── copilot.py
│   ├── database.py
│   ├── deep_research.py
│   ├── document_actions.py
│   ├── document_processor.py
│   ├── email_thread_parser.py
│   ├── embedding_lanes.py
│   ├── embeddings.py
│   ├── endpoint_resolver.py
│   ├── event_bus.py
│   ├── exceptions.py
│   ├── foreground_model_routing.py
│   ├── generated_images.py
│   ├── goal_based_extractor.py
│   ├── host_bridge_client.py
│   ├── host_docker_access.py
│   ├── image_model_ids.py
│   ├── index_walk.py
│   ├── integrations.py
│   ├── interactive_gate.py
│   ├── llm_core.py
│   ├── markitdown_runtime.py
│   ├── mcp_manager.py
│   ├── mcp_oauth.py
│   ├── memory_provider.py
│   ├── memory.py
│   ├── memory_vector.py
│   ├── model_capabilities.py
│   ├── model_capability_readers
│   │   ├── base.py
│   │   ├── generic_openai.py
│   │   ├── google_ai_studio_mapping.py
│   │   ├── google.py
│   │   ├── __init__.py
│   │   ├── llamacpp.py
│   │   ├── lmstudio.py
│   │   ├── ollama.py
│   │   ├── openai.py
│   │   └── openrouter.py
│   ├── model_context.py
│   ├── model_discovery.py
│   ├── office_doc.py
│   ├── optional_deps.py
│   ├── outbound_fetch.py
│   ├── owner_identity.py
│   ├── pdf_form_doc.py
│   ├── pdf_forms.py
│   ├── pdf_runtime.py
│   ├── personal_docs.py
│   ├── preset_manager.py
│   ├── prompt_security.py
│   ├── rag_manager.py
│   ├── rag_singleton.py
│   ├── rag_vector.py
│   ├── rate_limiter.py
│   ├── readiness.py
│   ├── reminder_personas.py
│   ├── request_models.py
│   ├── request_timing.py
│   ├── research_handler.py
│   ├── research_utils.py
│   ├── resource_guard.py
│   ├── runtime_paths.py
│   ├── search
│   │   ├── analytics.py
│   │   ├── cache.py
│   │   ├── content.py
│   │   ├── core.py
│   │   ├── __init__.py
│   │   ├── providers.py
│   │   ├── query.py
│   │   └── ranking.py
│   ├── secret_storage.py
│   ├── service_health.py
│   ├── session_actions.py
│   ├── session_image_cleanup.py
│   ├── session_search.py
│   ├── settings.py
│   ├── settings_scrub.py
│   ├── simple_intents.py
│   ├── task_action_policy.py
│   ├── task_endpoint.py
│   ├── task_scheduler.py
│   ├── teacher_escalation.py
│   ├── text_helpers.py
│   ├── tls_overrides.py
│   ├── tool_approval_scopes.py
│   ├── tool_approvals.py
│   ├── tool_capabilities.py
│   ├── tool_contracts.py
│   ├── tool_execution.py
│   ├── tool_implementations.py
│   ├── tool_index.py
│   ├── tool_parsing.py
│   ├── tool_policy.py
│   ├── tools
│   │   ├── calendar.py
│   │   ├── _common.py
│   │   ├── contacts.py
│   │   ├── cookbook.py
│   │   ├── image.py
│   │   ├── __init__.py
│   │   ├── notes.py
│   │   ├── research.py
│   │   ├── search.py
│   │   ├── system.py
│   │   └── vault.py
│   ├── tool_schemas.py
│   ├── tool_security.py
│   ├── tool_utils.py
│   ├── topic_analyzer.py
│   ├── upload_handler.py
│   ├── upload_limits.py
│   ├── url_safety.py
│   ├── url_security.py
│   ├── user_time.py
│   ├── vision_analyzer.py
│   ├── visual_report.py
│   ├── webhook_manager.py
│   └── youtube_handler.py
├── start-macos.sh
├── static
│   ├── app.js
│   ├── fonts
│   │   ├── custom
│   │   ├── FiraCode-Light.woff2
│   │   ├── FiraCode-Regular.woff2
│   │   ├── FiraCode-SemiBold.woff2
│   │   ├── Inter-Medium.woff2
│   │   ├── Inter-Regular.woff2
│   │   ├── Inter-SemiBold.woff2
│   │   ├── OpenDyslexic-Bold.woff2
│   │   └── OpenDyslexic-Regular.woff2
│   ├── icon.ico
│   ├── icons
│   │   ├── icon-192.png
│   │   ├── icon-512.png
│   │   ├── icon-maskable-512.png
│   │   ├── ollama-mark-crop.png
│   │   ├── ollama-mark.png
│   │   ├── sglang-logo.png
│   │   └── sglang-mark.png
│   ├── index.html
│   ├── js
│   │   ├── a11y.js
│   │   ├── admin.js
│   │   ├── appConfig.js
│   │   ├── assistant.js
│   │   ├── calendar
│   │   ├── calendar.js
│   │   ├── censor.js
│   │   ├── chatBusyState.mjs
│   │   ├── chat.js
│   │   ├── chatModelProvenance.js
│   │   ├── chatRenderer.js
│   │   ├── chatStreamErrors.js
│   │   ├── chatStream.js
│   │   ├── codeRunner.js
│   │   ├── color
│   │   ├── colorPicker.js
│   │   ├── compare
│   │   ├── composerArrowUpRecall.js
│   │   ├── cookbook-deps-recipes.js
│   │   ├── cookbook-diagnosis.js
│   │   ├── cookbookDownload.js
│   │   ├── cookbook-hwfit.js
│   │   ├── cookbook.js
│   │   ├── cookbookPorts.js
│   │   ├── cookbookProgressSignal.js
│   │   ├── cookbookRunning.js
│   │   ├── cookbookSchedule.js
│   │   ├── cookbookServe.js
│   │   ├── document.js
│   │   ├── documentLibrary.js
│   │   ├── dragSort.js
│   │   ├── editor
│   │   ├── emailInbox.js
│   │   ├── emailLibrary
│   │   ├── emailLibrary.js
│   │   ├── emailShared.js
│   │   ├── emojiPicker.js
│   │   ├── emojiShortcodes.js
│   │   ├── escMenuStack.js
│   │   ├── fileHandler.js
│   │   ├── galleryEditor.js
│   │   ├── gallery.js
│   │   ├── group.js
│   │   ├── init.js
│   │   ├── keyboard-shortcuts.js
│   │   ├── langIcons.js
│   │   ├── liveThinkingThrottle.js
│   │   ├── markdown
│   │   ├── markdown.js
│   │   ├── memory.js
│   │   ├── modalManager.js
│   │   ├── modalSnap.js
│   │   ├── model
│   │   ├── modelPicker.js
│   │   ├── models.js
│   │   ├── modelSort.js
│   │   ├── MODULE_SUMMARY.md
│   │   ├── notes.js
│   │   ├── package.json
│   │   ├── panels.js
│   │   ├── platform.js
│   │   ├── presets.js
│   │   ├── providerDeviceFlow.js
│   │   ├── providers.js
│   │   ├── rag.js
│   │   ├── research
│   │   ├── researchSynapse.js
│   │   ├── search-chat.js
│   │   ├── search.js
│   │   ├── section-management.js
│   │   ├── sessions.js
│   │   ├── settings
│   │   ├── settings.js
│   │   ├── sidebar-layout.js
│   │   ├── signature.js
│   │   ├── skills.js
│   │   ├── slashAutocomplete.js
│   │   ├── slashCommands.js
│   │   ├── spinner.js
│   │   ├── startupShell.js
│   │   ├── storage.js
│   │   ├── streamingRenderer.js
│   │   ├── streamingSegmenter.js
│   │   ├── tasks.js
│   │   ├── theme.js
│   │   ├── tileManager.js
│   │   ├── toolWindowZOrder.js
│   │   ├── tourAutoplay.js
│   │   ├── tourHints.js
│   │   ├── tts-ai.js
│   │   ├── ui.js
│   │   ├── ui_visibility.js
│   │   ├── util
│   │   ├── voiceRecorder.js
│   │   ├── windowDrag.js
│   │   ├── windowResize.js
│   │   └── workspace.js
│   ├── lib
│   │   ├── docx.umd.min.js
│   │   ├── highlight.min.js
│   │   ├── html2pdf.bundle.min.js
│   │   ├── katex
│   │   ├── mammoth.browser.min.js
│   │   ├── mermaid.min.js
│   │   ├── qrcode.min.js
│   │   └── xlsx.full.min.js
│   ├── login.html
│   ├── manifest.json
│   ├── modal-control-variants.html
│   ├── style.css
│   ├── sw.js
│   ├── wave-variants.html
│   └── whirlpool-variants.html
├── swift
│   └── odysseus-mlx-image-bridge
│       ├── Package.swift
│       └── Sources
├── tests
│   ├── bombadil-spec.ts
│   ├── cli
│   │   ├── test_calendar_cli_name.py
│   │   ├── test_contacts_cli_rows.py
│   │   ├── test_cookbook_cli_state.py
│   │   ├── test_docs_cli_content_length.py
│   │   ├── test_gallery_cli_album_count.py
│   │   ├── test_gallery_cli_preview.py
│   │   ├── test_logs_cli_resolve_nonstring.py
│   │   ├── test_mail_cli_read_empty_fetch.py
│   │   ├── test_mail_cli_recipients.py
│   │   ├── test_mcp_cli_env_serialize.py
│   │   ├── test_mcp_cli_json.py
│   │   ├── test_memory_cli_rows.py
│   │   ├── test_notes_cli_items.py
│   │   ├── test_personal_cli_rows.py
│   │   ├── test_preset_cli_invalid_entries.py
│   │   ├── test_preset_cli_set_corrupt_entry.py
│   │   ├── test_preset_cli_store.py
│   │   ├── test_research_cli_preview.py
│   │   ├── test_research_cli_status_filter.py
│   │   ├── test_research_cli_status.py
│   │   ├── test_research_cli_store.py
│   │   ├── test_sessions_cli.py
│   │   ├── test_signature_cli_export.py
│   │   ├── test_skills_cli_preview.py
│   │   ├── test_skills_cli_rows.py
│   │   ├── test_tasks_cli_preview.py
│   │   ├── test_theme_cli_store.py
│   │   └── test_webhook_cli_mask.py
│   ├── conftest.py
│   ├── helpers
│   │   ├── calendar_routes.py
│   │   ├── cli_loader.py
│   │   ├── db_stubs.py
│   │   ├── embedding_lanes.py
│   │   ├── import_state.py
│   │   ├── __init__.py
│   │   ├── sqlite_db.py
│   │   ├── test_settings_shell_coordinator.mjs
│   │   └── test_settings_shell.js
│   ├── __init__.py
│   ├── LAYOUT_INVENTORY.md
│   ├── live_thinking_scheduler.test.mjs
│   ├── markdown_codefence_placeholder_regression.mjs
│   ├── OVERSIZED_TEST_SPLIT_PLAN.md
│   ├── README.md
│   ├── run_focus.py
│   ├── run_order_report.py
│   ├── streaming
│   │   ├── corpus.mjs
│   │   ├── invariant.test.mjs
│   │   ├── markdownHarness.mjs
│   │   └── segmenter.test.mjs
│   ├── _taxonomy.py
│   ├── test_action_intents.py
│   ├── test_action_intents_shell_verbs.py
│   ├── test_active_document_clear.py
│   ├── test_active_email_reply_guard.py
│   ├── test_add_directory_event_loop.py
│   ├── test_admin_device_flow_static.py
│   ├── test_admin_tools_registry.py
│   ├── test_admin_wipe_gallery.py
│   ├── test_admin_wipe_routes_shim.py
│   ├── test_agent_bash_windows.py
│   ├── test_agent_computer_use_integration.py
│   ├── test_agent_loop.py
│   ├── test_agent_loop_tool_output_truncation.py
│   ├── test_agent_migration_manifest.py
│   ├── test_agent_round_model_provenance_ui.py
│   ├── test_agent_rounds_exhausted.py
│   ├── test_agent_state_dir_confinement.py
│   ├── test_agent_thread_dot_alignment_css.py
│   ├── test_agent_tool_budget_nonnumeric.py
│   ├── test_agent_tools_truncate_nonstring.py
│   ├── test_ai_image_url_safety.py
│   ├── test_ai_interaction_owner_scope.py
│   ├── test_amd_gpu_check_args.py
│   ├── test_anthropic_response_parse.py
│   ├── test_api_call_integration_routing.py
│   ├── test_api_chat_security.py
│   ├── test_api_key_file_permissions.py
│   ├── test_api_key_manager_atomic_save.py
│   ├── test_api_key_manager_corrupt_load.py
│   ├── test_api_key_manager_resilience.py
│   ├── test_api_token_routes.py
│   ├── test_api_token_tool_authority.py
│   ├── test_api_token_user_route_gate.py
│   ├── test_app_config_shared_fetch_js.py
│   ├── test_app_db_permissions.py
│   ├── test_app_initializer_memory_vector_degraded.py
│   ├── test_app.py
│   ├── test_approved_replay_message_shape.py
│   ├── test_app_static_mime.py
│   ├── test_archived_sessions_model_filter.py
│   ├── test_ask_user_persistence.py
│   ├── test_ask_user_tool.py
│   ├── test_atomic_io.py
│   ├── test_attachment_refs.py
│   ├── test_auth_config_lock_concurrency.py
│   ├── test_auth_disabled_document_access.py
│   ├── test_auth_event_loop.py
│   ├── test_auth_policy.py
│   ├── test_auth_regressions.py
│   ├── test_auth_require_privilege_nondict.py
│   ├── test_auth_root_path.py
│   ├── test_auth_session_revocation.py
│   ├── test_aux_llm_owner_scope.py
│   ├── test_background_llm_priority.py
│   ├── test_backup_cli_security.py
│   ├── test_backup_import_cross_user_dedup.py
│   ├── test_backup_import_skills_dedup.py
│   ├── test_backup_import_skills.py
│   ├── test_bg_jobs_store.py
│   ├── test_bg_job_tools.py
│   ├── test_bg_monitor_stream.py
│   ├── test_blind_compare_redaction.py
│   ├── test_budget_auto_sentinel.py
│   ├── test_build_user_content_pdf_marker.py
│   ├── test_builtin_actions_cookbook_serve_state.py
│   ├── test_builtin_actions_nonstring.py
│   ├── test_builtin_actions_owner_scope.py
│   ├── test_builtin_mcp_bg_tasks.py
│   ├── test_builtin_mcp_npx_cache.py
│   ├── test_builtin_mcp_pythonpath.py
│   ├── test_builtin_memory_consolidation.py
│   ├── test_cache_affinity_local_only.py
│   ├── test_caldav_bidirectional_sync.py
│   ├── test_caldav_client_cleanup.py
│   ├── test_caldav_google_principal_url.py
│   ├── test_caldav_prune_parse_failure.py
│   ├── test_caldav_redirect_hardening.py
│   ├── test_caldav_sync_prune_local_events.py
│   ├── test_caldav_sync_uid_scope.py
│   ├── test_caldav_test_connection_ssl.py
│   ├── test_caldav_url_hardening.py
│   ├── test_caldav_url_nonstring.py
│   ├── test_caldav_writeback.py
│   ├── test_caldav_writeback_route.py
│   ├── test_calendar_batch_events.py
│   ├── test_calendar_cli_overlap.py
│   ├── test_calendar_css_url_escape_js.py
│   ├── test_calendar_default_transaction.py
│   ├── test_calendar_event_contrast.py
│   ├── test_calendar_import_zero_duration.py
│   ├── test_calendar_list_range_aliases.py
│   ├── test_calendar_owner_scope.py
│   ├── test_calendar_parse_dt_naive.py
│   ├── test_calendar_parse_dt_time_first.py
│   ├── test_calendar_parse_dt_tonight.py
│   ├── test_calendar_recurrence.py
│   ├── test_calendar_reminder_minutes_parsing.py
│   ├── test_calendar_rrule.py
│   ├── test_calendar_rrule_until_utc.py
│   ├── test_calendar_update_event_tz.py
│   ├── test_calendar_utils_dates_js.py
│   ├── test_canvas_coords_empty_touches_js.py
│   ├── test_carddav_password_encryption.py
│   ├── test_censor_pref_js.py
│   ├── test_cerebras_cache_affinity.py
│   ├── test_chat_attachment_picker.py
│   ├── test_chat_busy_state.mjs
│   ├── test_chat_cached_model_normalization.py
│   ├── test_chatgpt_subscription_routes.py
│   ├── test_chat_helpers_bg_tasks_tracked.py
│   ├── test_chat_helpers.py
│   ├── test_chat_image_routing.py
│   ├── test_chat_metrics.py
│   ├── test_chat_model_provenance_js.py
│   ├── test_chat_preprocess_tool_policy.py
│   ├── test_chat_processor_pinned_memory.py
│   ├── test_chat_processor_web_search.py
│   ├── test_chat_route_tool_policy.py
│   ├── test_chat_stream_errors_js.py
│   ├── test_chat_stream_scope.py
│   ├── test_chat_tool_screenshot_xss.py
│   ├── test_chat_upload_limit_config.py
│   ├── test_chat_url_prefetch_failure_context.py
│   ├── test_checkin_digest_owner_scope.py
│   ├── test_check_outbound_url_nonstring.py
│   ├── test_chroma_client.py
│   ├── test_ci_authoritative_validation.py
│   ├── test_claim_ownerless_json.py
│   ├── test_classify_events_memory_text.py
│   ├── test_cleanup_owner_scope.py
│   ├── test_cleanup_routes_shim.py
│   ├── test_cleanup_service_utcnow.py
│   ├── test_code_nav_tools.py
│   ├── test_codex_cookbook_admin_gate.py
│   ├── test_codex_ssh_host_validation.py
│   ├── test_compaction_summary_failure.py
│   ├── test_compact_truncate_tool_call_args.py
│   ├── test_companion_pairing.py
│   ├── test_companion_readonly.py
│   ├── test_compare_ask_user_routing.py
│   ├── test_compare_endpoint_owner_scope.py
│   ├── test_compare_js.py
│   ├── test_compare_routes_shim.py
│   ├── test_compare_stop_disconnect_poll.py
│   ├── test_composer_arrow_up_recall_js.py
│   ├── test_compute_next_run_monthly_clamp.py
│   ├── test_computer_control_tools.py
│   ├── test_computer_use_controller.py
│   ├── test_computer_use_duplicate_prevention.py
│   ├── test_consolidate_memory_explicit_drops.py
│   ├── test_contacts_add_null_name.py
│   ├── test_contacts_carddav_security.py
│   ├── test_contacts_import_nonstring.py
│   ├── test_contacts_routes_shim.py
│   ├── test_contacts_vcard_parse.py
│   ├── test_context_budget.py
│   ├── test_context_cache_per_endpoint.py
│   ├── test_context_compactor_nonstring.py
│   ├── test_context_compactor.py
│   ├── test_cookbook_agent_tool_ssh_validation.py
│   ├── test_cookbook_cpu_only_serve.py
│   ├── test_cookbook_dead_download_status.py
│   ├── test_cookbook_dependency_completion_regression.py
│   ├── test_cookbook_deps_recipes.py
│   ├── test_cookbook_diagnosis_js.py
│   ├── test_cookbook_diagnosis.py
│   ├── test_cookbook_docker_access.py
│   ├── test_cookbook_download_toast_duration.py
│   ├── test_cookbook_endpoint_registration.py
│   ├── test_cookbook_error_feedback.py
│   ├── test_cookbook_error_tail_lines.py
│   ├── test_cookbook_gemma4_thinking_template.py
│   ├── test_cookbook_helpers.py
│   ├── test_cookbook_hf_token.py
│   ├── test_cookbook_local_serve_pid_winpid.py
│   ├── test_cookbook_package_detection.py
│   ├── test_cookbook_port_parsing_js.py
│   ├── test_cookbook_progress_signal_js.py
│   ├── test_cookbook_remote_windows_diffusers.py
│   ├── test_cookbook_same_host_server_profiles_js.py
│   ├── test_cookbook_serve_lifecycle.py
│   ├── test_cookbook_windows_stop_tree_js.py
│   ├── test_copilot.py
│   ├── test_copilot_routes.py
│   ├── test_copy_message_strips_thinking_js.py
│   ├── test_cors_preflight.py
│   ├── test_database_utcnow.py
│   ├── test_db_stubs_helper.py
│   ├── test_ddg_redirect_resolution.py
│   ├── test_deep_research_date_context.py
│   ├── test_deep_research_extraction_controls.py
│   ├── test_deep_research_parse_json_array_echo.py
│   ├── test_deep_research_search_error.py
│   ├── test_deep_research_synthesis_resilience.py
│   ├── test_deleted_session_sidebar_regression.py
│   ├── test_delete_message_no_session.py
│   ├── test_delete_user_invalidates_token_cache.py
│   ├── test_delete_user_revokes_api_tokens.py
│   ├── test_derive_title_nonstring.py
│   ├── test_device_flow_routes.py
│   ├── test_diagnostics_logs.py
│   ├── test_diagnostics_service_route.py
│   ├── test_dialog_aria.py
│   ├── test_diffusion_server_security.py
│   ├── test_digest_windows.py
│   ├── test_direct_site_navigation.py
│   ├── test_direct_upload_limits.py
│   ├── test_docker_devops_hardening.py
│   ├── test_doc_library_open_orphaned.py
│   ├── test_docs_no_orphan_images.py
│   ├── test_docs_query_nondict_rows.py
│   ├── test_document_actions_nonstring.py
│   ├── test_document_ai_preview_refresh_js.py
│   ├── test_document_close_clears_active_route.py
│   ├── test_document_deeplink.py
│   ├── test_document_diff_discard_on_update_js.py
│   ├── test_document_editor_scroll.py
│   ├── test_document_library_delete_counters.py
│   ├── test_document_library_language_facet.py
│   ├── test_document_library_pdf_metadata.py
│   ├── test_document_pdf_marker.py
│   ├── test_document_processor_attachment_budget.py
│   ├── test_document_processor_empty_media_subtype.py
│   ├── test_document_render_pdf_iframe.py
│   ├── test_document_routes_shim.py
│   ├── test_document_session_owner_scope.py
│   ├── test_document_tidy_null_timestamp.py
│   ├── test_document_tool_owner_scope.py
│   ├── test_edit_file.py
│   ├── test_editor_draft_payload.py
│   ├── test_email_account_default_serialization.py
│   ├── test_email_account_port_validation.py
│   ├── test_email_account_secret_storage.py
│   ├── test_email_decode_header.py
│   ├── test_email_envelope_recipients.py
│   ├── test_email_fallback_reconnect.py
│   ├── test_email_gmail_fetch_flags.py
│   ├── test_email_helpers_decode_header_spaces.py
│   ├── test_email_imap_timeout.py
│   ├── test_email_library_bulk_actions.py
│   ├── test_email_library_prewarm.py
│   ├── test_email_linkify_security_js.py
│   ├── test_email_oauth_connect_smtp_security.py
│   ├── test_email_oauth_docker_config.py
│   ├── test_email_oauth.py
│   ├── test_email_oauth_settings_redirect.py
│   ├── test_email_open_dedup_js.py
│   ├── test_email_ownerless_account_owner_scope.py
│   ├── test_email_owner_scope.py
│   ├── test_email_polly_imap_leak.py
│   ├── test_email_read_mark_seen.py
│   ├── test_email_registry_sync.py
│   ├── test_email_send_only_no_inbox.py
│   ├── test_email_smtp_security.py
│   ├── test_email_split_border_css.py
│   ├── test_email_summary_error_ui_js.py
│   ├── test_email_summary_llm.py
│   ├── test_email_test_connection_oauth.py
│   ├── test_email_thread_parser_nonstring.py
│   ├── test_email_uid_no_seqno_fallback.py
│   ├── test_email_unsubscribe_candidates.py
│   ├── test_email_urgency_checkpoint.py
│   ├── test_embedding_cache_confinement.py
│   ├── test_embedding_endpoint_config.py
│   ├── test_embedding_lane_ndarray_restore.py
│   ├── test_embedding_lanes_legacy.py
│   ├── test_embedding_lanes_memory.py
│   ├── test_embedding_lanes.py
│   ├── test_embedding_lanes_rag.py
│   ├── test_embedding_lanes_tool_index.py
│   ├── test_embeddings_client.py
│   ├── test_embeddings.py
│   ├── test_emoji_shortcodes_js.py
│   ├── test_emoji_svg_hardening.py
│   ├── test_endpoint_owner_scope_followup.py
│   ├── test_endpoint_probing.py
│   ├── test_endpoint_resolver_headers.py
│   ├── test_endpoint_resolver_models.py
│   ├── test_endpoint_resolver_urls.py
│   ├── test_esc_menu_stack_js.py
│   ├── test_estimate_tokens_tool_calls.py
│   ├── test_explicit_memory.py
│   ├── test_external_context_tool_gate.py
│   ├── test_extract_quotes.py
│   ├── test_extract_skill_json_nonstring.py
│   ├── test_extract_statistics.py
│   ├── test_extract_urls.py
│   ├── test_fastembed_cache_path.py
│   ├── test_fenced_example_not_executed_for_native_models.py
│   ├── test_fenced_inline_args.py
│   ├── test_fenced_invoke_no_raw_xml.py
│   ├── test_focus_before_input.py
│   ├── test_focused_test_guidance.py
│   ├── test_font_routes.py
│   ├── test_foreground_model_routing.py
│   ├── test_fork_session_metadata.py
│   ├── test_form_markdown_roundtrip.py
│   ├── test_forwarded_message_divider.py
│   ├── test_function_call_non_object_args.py
│   ├── test_function_model_tool_call.py
│   ├── test_gallery_album_owner_scope.py
│   ├── test_gallery_delete_file_ordering.py
│   ├── test_gallery_endpoint_hardening.py
│   ├── test_gallery_endpoint_matching.py
│   ├── test_gallery_endpoint_ssrf.py
│   ├── test_gallery_exif_orientation.py
│   ├── test_gallery_filename_confinement.py
│   ├── test_gallery_image_endpoint_owner_scope.py
│   ├── test_gallery_image_privileges.py
│   ├── test_gallery_model_input_device.py
│   ├── test_gallery_null_user_routes.py
│   ├── test_gallery_owner_filter_single_user.py
│   ├── test_gallery_result_image_ssrf.py
│   ├── test_gallery_routes_shim.py
│   ├── test_gemma_tool_call_parsing.py
│   ├── test_generated_image_confinement.py
│   ├── test_gmail_quote_attribution_js.py
│   ├── test_gpu_compose_standalone.py
│   ├── test_group_character_dropdown.py
│   ├── test_group_chat_storage.py
│   ├── test_gui_input_tools.py
│   ├── test_harmonize_masks_invalid_layers_js.py
│   ├── test_harmony_tool_aliasing.py
│   ├── test_helpers_import_state.py
│   ├── test_hex_to_rgb_js.py
│   ├── test_history_compact_tool_calls.py
│   ├── test_history_db_fallback_hidden.py
│   ├── test_history_display_model_hydration.py
│   ├── test_history_order_by_timestamp_regression.py
│   ├── test_history_routes_shim.py
│   ├── test_history_topics_owner_scope.py
│   ├── test_host_bridge_lifecycle.py
│   ├── test_host_bridge.py
│   ├── test_host_bridge_token_storage.py
│   ├── test_hwfit_amd.py
│   ├── test_hwfit_apple_bandwidth.py
│   ├── test_hwfit_bandwidth_nonstring.py
│   ├── test_hwfit_container_visibility_warning.py
│   ├── test_hwfit_cpu_arch_detection.py
│   ├── test_hwfit_cpu_only_fallback.py
│   ├── test_hwfit_gemma4_12b.py
│   ├── test_hwfit_gpu_count_nonnumeric.py
│   ├── test_hwfit_macos.py
│   ├── test_hwfit_manual_backend.py
│   ├── test_hwfit_models_nonstring_fields.py
│   ├── test_hwfit_native_quant_labels.py
│   ├── test_hwfit_params_b_malformed.py
│   ├── test_hwfit_quant_formats.py
│   ├── test_hwfit_remote_validation.py
│   ├── test_hwfit_unified_nvidia.py
│   ├── test_hwfit_windows.py
│   ├── test_icloud_imap_full_fetch.py
│   ├── test_ics_escape.py
│   ├── test_ics_export_escaping.py
│   ├── test_ics_import_dedup_tz.py
│   ├── test_image_models_nondict_system.py
│   ├── test_image_models_nonstring_search.py
│   ├── test_imap_leak_fixes.py
│   ├── test_imap_mailbox_quoting.py
│   ├── test_imap_move_uid.py
│   ├── test_imap_uid_commands.py
│   ├── TESTING_STANDARD.md
│   ├── test_inside_base_dir_nonstring.py
│   ├── test_integration_api_call_ssrf.py
│   ├── test_integrations_api_call_truncation.py
│   ├── test_integrations_store_shape.py
│   ├── test_integrations_url_join.py
│   ├── test_interactive_gate_passive_paths.py
│   ├── test_internal_api_base.py
│   ├── test_issue_description_check.py
│   ├── test_is_youtube_url_nonstring.py
│   ├── test_is_youtube_url_nonstring_svc.py
│   ├── test_jarvis_desktop_hotkey_integration.py
│   ├── test_jarvis_vision_phase.py
│   ├── test_keybind_altgr_js.py
│   ├── test_kimi_code_hosts.py
│   ├── test_kimi_code_user_agent.py
│   ├── test_kokoro_optional_requirements.py
│   ├── test_kv_cache_invalidation_2927.py
│   ├── test_lang_icon_null_opts_js.py
│   ├── test_launcher.py
│   ├── test_lazy_mcp.py
│   ├── test_legacy_default_fallback_ui.py
│   ├── test_live_fallback_round_attribution.py
│   ├── test_live_strip_email_tool_fences.py
│   ├── test_live_thinking_chat_integration.py
│   ├── test_live_thinking_scheduler_js.py
│   ├── test_llamacpp_discovery.py
│   ├── test_llama_server_models_url.py
│   ├── test_llm_core_anthropic_cache.py
│   ├── test_llm_core_anthropic_temp_clamp.py
│   ├── test_llm_core_anthropic_temp_omit.py
│   ├── test_llm_core_async_mistral_content.py
│   ├── test_llm_core_concurrency.py
│   ├── test_llm_core_connect_timeout.py
│   ├── test_llm_core_fallback.py
│   ├── test_llm_core_mistral_content.py
│   ├── test_llm_core_ollama.py
│   ├── test_llm_core_ollama_thinking.py
│   ├── test_llm_core_openai_reasoning_tools.py
│   ├── test_llm_core_reasoning_content_fallback.py
│   ├── test_llm_core_reasoning.py
│   ├── test_llm_core_sanitize_tool_calls.py
│   ├── test_llm_core_sse_no_space.py
│   ├── test_llm_core_streaming.py
│   ├── test_llm_core_system_msg_missing_content.py
│   ├── test_llm_core_temperature_anthropic.py
│   ├── test_llm_core_temperature_moonshot.py
│   ├── test_llm_core_temperature_reasoning.py
│   ├── test_llm_core_thinking_models.py
│   ├── test_llm_core_usage_finish_delta.py
│   ├── test_lmstudio_discovery.py
│   ├── test_lmstudio_models_url.py
│   ├── test_lmstudio_vision.py
│   ├── test_load_features_permission_error.py
│   ├── test_local_endpoint_api_key_js.py
│   ├── test_local_endpoint_js.py
│   ├── test_log_safety.py
│   ├── test_loop_breaker_runaway.py
│   ├── test_manage_mcp_command_allowlist.py
│   ├── test_manage_memory_list.py
│   ├── test_manage_notes_owner_gate.py
│   ├── test_manage_settings_token_budget.py
│   ├── test_manage_skills_action_required.py
│   ├── test_manage_tasks_owner_scope.py
│   ├── test_markdown_dom_xss_helpers.py
│   ├── test_markdown_lazy_lib_loading_js.py
│   ├── test_markdown_rendering_js.py
│   ├── test_markdown_table_row_js.py
│   ├── test_markitdown_format_nonstring.py
│   ├── test_markitdown_runtime.py
│   ├── test_matchescombo_nonstring_js.py
│   ├── test_match_model_key_js.py
│   ├── test_mcp_cache_invalidation.py
│   ├── test_mcp_common_truncate.py
│   ├── test_mcp_dependency_compatibility.py
│   ├── test_mcp_email_decode_header_spaces.py
│   ├── test_mcp_manager.py
│   ├── test_mcp_memory_owner_scope.py
│   ├── test_mcp_oauth.py
│   ├── test_mcp_oauth_secure_storage.py
│   ├── test_mcp_param_hint_hardening.py
│   ├── test_mcp_reconnect_args.py
│   ├── test_mcp_routes_shim.py
│   ├── test_mcp_tool_params_in_prompt.py
│   ├── test_memory_add_submit_regression.py
│   ├── test_memory_audit_timeout.py
│   ├── test_memory_bullet_extraction.py
│   ├── test_memory_cli_add_nondict.py
│   ├── test_memory_extract_chat_nondict.py
│   ├── test_memory_extraction_parse.py
│   ├── test_memory_extractor_rows.py
│   ├── test_memory_extractor_vector_cross_tenant.py
│   ├── test_memory_extractor_vector_degraded.py
│   ├── test_memory_fallback_dislike.py
│   ├── test_memory_imports.py
│   ├── test_memory_owner_isolation.py
│   ├── test_memory_provider.py
│   ├── test_memory_recall_nondict_rows.py
│   ├── test_memory_routes_session_owner.py
│   ├── test_memory_routes_shim.py
│   ├── test_memory_store_unreadable_no_wipe.py
│   ├── test_memory_validate_entries_nondict.py
│   ├── test_merge_last_assistant_rows.py
│   ├── test_migrate_faiss_to_chroma.py
│   ├── test_misfenced_read_file_tool_call.py
│   ├── test_mlx_image_server_security.py
│   ├── test_modal_dock_composer_clearance.py
│   ├── test_model_capabilities.py
│   ├── test_model_capability_readers.py
│   ├── test_model_context.py
│   ├── test_model_defaults.py
│   ├── test_model_discovery_status.py
│   ├── test_model_helper_owner_scope.py
│   ├── test_model_interaction_registry.py
│   ├── test_model_name_tooltip.py
│   ├── test_model_routes.py
│   ├── test_model_sort_js.py
│   ├── test_multiple_mcp_servers_timeout.py
│   ├── test_native_tool_result_threading.py
│   ├── test_new_chat_clears_input.py
│   ├── test_new_chat_model_preference.py
│   ├── test_nix_upload_text.py
│   ├── test_note_reminder_email_oauth.py
│   ├── test_note_reminder_fire_scope.py
│   ├── test_note_routes_shim.py
│   ├── test_notes_dom_xss_helpers.py
│   ├── test_notes_fail_closed_auth.py
│   ├── test_notes_search_reset_on_reopen_js.py
│   ├── test_notes_select_esc_listener_js.py
│   ├── test_notes_update_due_date.py
│   ├── test_notes_z_order_js.py
│   ├── test_ntfy_tools.py
│   ├── test_null_owner_gates.py
│   ├── test_odysseus_dispatcher.py
│   ├── test_odysseus_doc_fence_normalization.py
│   ├── test_og_image_extraction.py
│   ├── test_ollama_multimodal.py
│   ├── test_ollama_port_detection.py
│   ├── test_ollama_runner_hint.py
│   ├── test_ordinal_suffix_js.py
│   ├── test_owned_document_query.py
│   ├── test_owner_identity.py
│   ├── test_panel_loader_js.py
│   ├── test_parse_due_time_first.py
│   ├── test_parse_msg_content_jsonlike_string.py
│   ├── test_pdf_ai_edit_derivative.py
│   ├── test_pdf_runtime.py
│   ├── test_personal_delete_file_confinement.py
│   ├── test_personal_dir_symlink_escape.py
│   ├── test_personal_docs_exclusions.py
│   ├── test_personal_docs_keyword_nondict.py
│   ├── test_personal_docs_lists.py
│   ├── test_personal_docs_office_index.py
│   ├── test_personal_docs_pdf_index.py
│   ├── test_personal_docs_state_store.py
│   ├── test_personal_index_hidden_dirs.py
│   ├── test_personal_remove_dir_confinement.py
│   ├── test_personal_upload_isolation.py
│   ├── test_personal_upload_privilege.py
│   ├── test_plain_ui_control_open_panel.py
│   ├── test_plan_mode.py
│   ├── test_platform_compat.py
│   ├── test_poll_endpoint_no_task_interrupt.py
│   ├── test_popup_opener_isolation_js.py
│   ├── test_portal_dropdown_z_js.py
│   ├── test_pr6020_browser_review_regressions.py
│   ├── test_pr6020_rebase_regressions.py
│   ├── test_pr_blocker_audit.py
│   ├── test_pr_description_check.py
│   ├── test_prefs_atomic_write.py
│   ├── test_prefs_routes.py
│   ├── test_prefs_single_user_no_clobber.py
│   ├── test_preset_atomic_save.py
│   ├── test_preset_expand_owner_scope.py
│   ├── test_preset_fill_missing_defaults.py
│   ├── test_preset_local_storage_js.py
│   ├── test_preset_store_shape.py
│   ├── test_promote_image_fields.py
│   ├── test_prompt_injection_audit.py
│   ├── test_prompt_security.py
│   ├── test_provider_classification_errors.py
│   ├── test_provider_classification.py
│   ├── test_provider_classification_token_params.py
│   ├── test_provider_detection_builders.py
│   ├── test_provider_detection_detect.py
│   ├── test_provider_detection_host_match.py
│   ├── test_provider_device_flow_js.py
│   ├── test_provider_endpoints_headers.py
│   ├── test_provider_endpoints_models.py
│   ├── test_provider_endpoints_normalization.py
│   ├── test_provider_endpoints_tailscale.py
│   ├── test_provider_endpoints_url_building.py
│   ├── test_provider_label_js.py
│   ├── test_providers_mixtral_logo_js.py
│   ├── test_public_blocked_tool_nonstring.py
│   ├── test_question_type_detection.py
│   ├── test_rag_index_hidden_dirs.py
│   ├── test_rag_keyword_fallback_owner.py
│   ├── test_rag_manager_owner_compat.py
│   ├── test_rag_remove_directory_scope.py
│   ├── test_rag_search_signature.py
│   ├── test_rag_server_directory_nonstring.py
│   ├── test_rag_vector_id_stability.py
│   ├── test_rag_vector_rename_owner.py
│   ├── test_rate_limiter.py
│   ├── test_readiness.py
│   ├── test_readme_ascii_fenced.py
│   ├── test_realesrgan_torchvision_compat.py
│   ├── test_redos_cal_extract.py
│   ├── test_redos_llm_parsers.py
│   ├── test_redos_think_blocks.py
│   ├── test_redos_verdict_continuation.py
│   ├── test_redos_xml_tool_parsers.py
│   ├── test_reminder_ntfy_ssrf.py
│   ├── test_rename_user_case_insensitive.py
│   ├── test_rename_user_owner_sync.py
│   ├── test_rename_user_token_cache.py
│   ├── test_replace_messages_multimodal.py
│   ├── test_replace_messages_upload_reservations.py
│   ├── test_reply_all_cc_nonstring_js.py
│   ├── test_reply_recipients_js.py
│   ├── test_request_timing.py
│   ├── test_research_chat_stream_owner.py
│   ├── test_research_endpoint_owner_scope.py
│   ├── test_research_handler_analyzed_urls.py
│   ├── test_research_handler_path_confinement.py
│   ├── test_research_handler_raw_nondict.py
│   ├── test_research_handler_sources_nondict.py
│   ├── test_research_owner_scope_routes.py
│   ├── test_research_probe_errors.py
│   ├── test_research_query_fallback.py
│   ├── test_research_report_read.py
│   ├── test_research_routes_path_confinement.py
│   ├── test_research_routes_shim.py
│   ├── test_research_service.py
│   ├── test_research_session_id_validation.py
│   ├── test_research_source_link_xss.py
│   ├── test_research_status_avg_duration.py
│   ├── test_research_utils_low_quality_nonstring.py
│   ├── test_research_utils.py
│   ├── test_resend_message_nondestructive.py
│   ├── test_reserved_username_admin_escalation.py
│   ├── test_resolve_endpoint_fallbacks.py
│   ├── test_resolve_model_offloaded.py
│   ├── test_resolve_session_auth_chatgpt.py
│   ├── test_resolve_upload_path_nondict.py
│   ├── test_retired_settings_interfaces.py
│   ├── test_review_regressions.py
│   ├── test_rewrite_persist_column.py
│   ├── test_route_validators.py
│   ├── test_run_focus.py
│   ├── test_run_order_report.py
│   ├── test_runtime_paths.py
│   ├── test_sanitize_multimodal_merge.py
│   ├── test_sanitize_preserves_reasoning.py
│   ├── test_scheduled_poll_race.py
│   ├── test_schedule_email_offset_normalization.py
│   ├── test_scheduler_prompt_cache_time.py
│   ├── test_scheduler_restart_doublefire.py
│   ├── test_scheduler_scheduled_time_validation.py
│   ├── test_search_analytics_defaults.py
│   ├── test_search_cache_invalidation.py
│   ├── test_search_config_no_key_leak.py
│   ├── test_search_config_provider_key.py
│   ├── test_search_content_block_source_index.py
│   ├── test_search_content_extraction_parity.py
│   ├── test_search_content_url_guards.py
│   ├── test_search_module_consolidation.py
│   ├── test_search_provider_json.py
│   ├── test_search_query_entities_nonstring.py
│   ├── test_search_query_nonstring.py
│   ├── test_search_query.py
│   ├── test_search_query_unicode_names.py
│   ├── test_search_ranking.py
│   ├── test_search_ranking_recency.py
│   ├── test_search_ranking_sports_substring.py
│   ├── test_search_ranking_subject_substring.py
│   ├── test_search_routes_shim.py
│   ├── test_search_service_nondict_rows.py
│   ├── test_searchservice_search_call.py
│   ├── test_searxng_image_pinned.py
│   ├── test_searxng_settings_migration.py
│   ├── test_secure_files_windows.py
│   ├── test_security_headers_middleware.py
│   ├── test_security_headers_pdf_preview.py
│   ├── test_security_regressions.py
│   ├── test_select_dropdown_theme_css.py
│   ├── test_sender_signature_skip_roles.py
│   ├── test_serve_html_with_nonce.py
│   ├── test_serve_profiles.py
│   ├── test_service_health_chromadb.py
│   ├── test_service_health_collect.py
│   ├── test_service_health_email.py
│   ├── test_service_health_ntfy.py
│   ├── test_service_health_providers.py
│   ├── test_service_health_search.py
│   ├── test_service_search_provider_guards.py
│   ├── test_services_research_low_quality_sources.py
│   ├── test_services_search_analytics_defaults.py
│   ├── test_session_actions_cleanup.py
│   ├── test_session_concurrent.py
│   ├── test_session_context_excludes_slash.py
│   ├── test_session_discovery_message_count.py
│   ├── test_session_endpoint_owner_scope.py
│   ├── test_session_export_filename.py
│   ├── test_session_export_nonstring_content.py
│   ├── test_session_ghost_delete.py
│   ├── test_session_image_cleanup.py
│   ├── test_session_list_owner_scope.py
│   ├── test_session_manager_cleanup.py
│   ├── test_session_manager_persist_guard.py
│   ├── test_session_manager.py
│   ├── test_session_mode_helpers.py
│   ├── test_session_owner_attribution.py
│   ├── test_session_routes_utcnow.py
│   ├── test_session_search_batch_fetch.py
│   ├── test_session_search.py
│   ├── test_session_tools_registry.py
│   ├── test_set_admin.py
│   ├── test_settings_error_paths.py
│   ├── test_settings_scrub.py
│   ├── test_settings_shell_js_behavior.py
│   ├── test_settings_store_shape.py
│   ├── test_setup_admin_user.py
│   ├── test_setup_device_auth_static.py
│   ├── test_setup_llamacpp_hint_js.py
│   ├── test_shell_routes.py
│   ├── test_shell_service.py
│   ├── test_signature_fold_js.py
│   ├── test_signature_fold_self_closing_br_js.py
│   ├── test_signature_route_hardening.py
│   ├── test_signature_settings_dom_xss.py
│   ├── test_simple_assistant.py
│   ├── test_skill_edit_no_collapse_on_outside_click_js.py
│   ├── test_skill_extractor_json.py
│   ├── test_skill_extractor_rows.py
│   ├── test_skill_extractor_stray_brace.py
│   ├── test_skill_format_timestamp.py
│   ├── test_skill_frontmatter_escape_roundtrip.py
│   ├── test_skill_importer_dns_pinning.py
│   ├── test_skill_importer.py
│   ├── test_skill_importer_security.py
│   ├── test_skill_importer_ssrf_redirect.py
│   ├── test_skill_index_prompt_injection.py
│   ├── test_skill_index_toolset_gating.py
│   ├── test_skill_save_no_rename.py
│   ├── test_skills_delete_owner.py
│   ├── test_skills_manager_owner_isolation.py
│   ├── test_skills_routes_nondict.py
│   ├── test_skills_routes_owner_update.py
│   ├── test_skills_tag_token_match.py
│   ├── test_slash_autocomplete_static.py
│   ├── test_slash_setup_provider_aliases.py
│   ├── test_snap_other_layers_nonarray_js.py
│   ├── test_speech_service_toggles.py
│   ├── test_spinner_stops_when_never_attached_js.py
│   ├── test_split_chunks_no_duplicate_tail.py
│   ├── test_sqlite_foreign_keys.py
│   ├── test_src_search_query_nonstring.py
│   ├── test_startup_session_bootstrap_js.py
│   ├── test_startup_shell_js.py
│   ├── test_streaming_segmenter_js.py
│   ├── test_strip_reasoning_prose_dataloss.py
│   ├── test_strip_think.py
│   ├── test_stt_audio_formats.py
│   ├── test_stt_leak.py
│   ├── test_stt_local_provider.py
│   ├── test_svc_research_sources_nondict.py
│   ├── test_tailscale_discovery_cache.py
│   ├── test_task_chain_owner_scope.py
│   ├── test_task_cookbook_admin_gate.py
│   ├── test_task_endpoint_normalization.py
│   ├── test_task_routes_shim.py
│   ├── test_task_scheduler_cache.py
│   ├── test_task_scheduler_cancel.py
│   ├── test_task_scheduler_session_delivery.py
│   ├── test_task_session_folder.py
│   ├── test_task_shell_tools.py
│   ├── test_taxonomy.py
│   ├── test_teacher_audit_owner_scope.py
│   ├── test_teacher_eval_nonstring_reply.py
│   ├── test_teacher_eval_tier2.py
│   ├── test_tidy_research_owner_scope.py
│   ├── test_tile_manager_snap_zones_js.py
│   ├── test_tls_overrides_scope.py
│   ├── test_toast_dismiss_pointer_events.py
│   ├── test_tool_approval_frontend_routing.py
│   ├── test_tool_approval_single_action_scope.py
│   ├── test_tool_approvals.py
│   ├── test_tool_approval_task_scope.py
│   ├── test_tool_contracts.py
│   ├── test_tool_execution_navigation_guard.py
│   ├── test_tool_implementations_shim.py
│   ├── test_tool_index_keyword_boundaries.py
│   ├── test_tool_index_schema_parity.py
│   ├── test_tool_output_prompt_injection.py
│   ├── test_tool_parsing_bare_end_marker.py
│   ├── test_tool_parsing_hermes_json.py
│   ├── test_tool_parsing_nonstring.py
│   ├── test_tool_path_confinement.py
│   ├── test_tool_policy.py
│   ├── test_tool_rag_contacts_domain.py
│   ├── test_tool_rag_keyword_hints.py
│   ├── test_tool_support_heuristic.py
│   ├── test_tool_task_cancelled_on_disconnect.py
│   ├── test_tool_utils_import_clean.py
│   ├── test_topic_analyzer.py
│   ├── test_totp_failclosed.py
│   ├── test_truncate_message_count_regression.py
│   ├── test_tts_available_nonstring_provider.py
│   ├── test_tts_cache_stats.py
│   ├── test_tts_service_enforce_cache_limit.py
│   ├── test_tts_speed_malformed.py
│   ├── test_ui_control_rag_toggle.py
│   ├── test_ui_visibility_js.py
│   ├── test_unknown_tool_calls.py
│   ├── test_update_database_script.py
│   ├── test_update_plan_tool.py
│   ├── test_upload_content_detection_magic.py
│   ├── test_upload_error_surfaced.py
│   ├── test_upload_handler_atomicity.py
│   ├── test_upload_handler_cleanup.py
│   ├── test_upload_handler_rename_owner.py
│   ├── test_upload_id_extension.py
│   ├── test_upload_id_validation.py
│   ├── test_upload_limits_centralized.py
│   ├── test_upload_multifile.py
│   ├── test_upload_routes_owner_scope.py
│   ├── test_url_safety.py
│   ├── test_user_time.py
│   ├── test_vault_password_not_in_argv.py
│   ├── test_vault_routes_shim.py
│   ├── test_vcard_unfolding.py
│   ├── test_venice_hosts.py
│   ├── test_vision_model_detection.py
│   ├── test_vision_owner_scope.py
│   ├── test_visual_report_icon_url.py
│   ├── test_visual_report_nonstring.py
│   ├── test_visual_report.py
│   ├── test_visual_report_slug_unique.py
│   ├── test_visual_report_toc_code_fence.py
│   ├── test_voice_first_flow.py
│   ├── test_warmup_ping_urls.py
│   ├── test_web_fetch_plaintext.py
│   ├── test_web_fetch_size_caps.py
│   ├── test_webhook_dns_rebinding_pin.py
│   ├── test_webhook_emitters_use_manager.py
│   ├── test_webhook_routes_shim.py
│   ├── test_webhook_sanitize_error_ipv6.py
│   ├── test_webhook_ssrf_resilience.py
│   ├── test_webhook_task_refs.py
│   ├── test_webhook_trigger_auth_exempt.py
│   ├── test_web_search_query_sanitization.py
│   ├── test_web_search_raw_json_tool_call.py
│   ├── test_web_search_time_filter.py
│   ├── test_web_search_tool_icon_js.py
│   ├── test_web_search_tool.py
│   ├── test_web_user_agent_constant.py
│   ├── test_windows_update_script.py
│   ├── test_workspace_confine.py
│   ├── test_youtube_comments_timeout.py
│   ├── test_youtube_extract_id_nonstring.py
│   ├── test_youtube_handler_consolidation.py
│   ├── test_youtube_svc_comments_nondict.py
│   ├── test_youtube_transcript_seg_nondict.py
│   └── tools
│       └── build_oversized_test_split_plan.py
├── THREAT_MODEL.md
├── tmp_pytest_probe
├── tree.md
├── update_windows.bat
└── website
    ├── agent-migration.md
    ├── attachments.md
    ├── backup-restore.md
    ├── bg.webm
    ├── chat.webm
    ├── compare.webm
    ├── _config.yml
    ├── document.webm
    ├── email-outlook.md
    ├── gallery.webm
    ├── index.html
    ├── notes.webm
    ├── pr-blocker-audit.md
    ├── research.webm
    ├── security-ci.md
    ├── setup.md
    └── theme.webm

132 directories, 1588 files
