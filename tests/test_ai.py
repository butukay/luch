"""Offline contract tests against a real streaming HTTP server."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest

from luch.ai import AiClient


class HTTPTest(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.responses = []
        self.request_headers = []
        self.paths = []
        self.redirect_to = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.append(json.loads(body))
                owner.request_headers.append(dict(self.headers))
                owner.paths.append(self.path)
                status, events = owner.responses.pop(0)
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream" if owner.client.api_format == "openai" else "application/x-ndjson")
                self.end_headers()
                for event in events:
                    if owner.client.api_format == "openai":
                        data = event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)
                        self.wfile.write(("data: " + data + "\n\n").encode())
                    else:
                        self.wfile.write(json.dumps(event).encode() + b"\n")
                    self.wfile.flush()

            def do_GET(self):
                owner.paths.append(self.path)
                owner.request_headers.append(dict(self.headers))
                if owner.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", owner.redirect_to)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"data": [{"id": owner.client.model}]}).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = AiClient({"endpoint": f"http://127.0.0.1:{self.server.server_port}",
                                "api_format": getattr(self, "api_format", "ollama")})
        self.events = []
        self.cancelled = threading.Event()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class AiClientTest(HTTPTest):

    def answer(self, *content):
        events = [{"message": {"content": text}, "done": False} for text in content]
        events.append({"done": True, "eval_count": 4, "eval_duration": 20_000_000})
        self.responses.append((200, events))

    def command(self, command="free -h", explanation="Проверить память", **extras):
        self.responses.append((200, [{"message": {"tool_calls": [{"function": {
            "name": "shell", "arguments": {"command": command, "explanation": explanation, **extras},
        }}]}, "done": True}]))

    def test_streams_russian_answer_and_reports_latency(self):
        self.answer("Привет", "!")
        self.client.stream("Здравствуй", self.events.append, self.cancelled)
        self.assertEqual("Привет!", "".join(e.get("text", "") for e in self.events))
        self.assertEqual("done", self.events[-1]["type"])
        self.assertFalse(self.events[-1]["awaiting_command"])
        self.assertEqual(200, self.events[-1]["stats"]["tokens_per_second"])
        self.assertFalse(self.requests[0]["think"])
        self.assertEqual(-1, self.requests[0]["keep_alive"])

    def test_command_is_only_proposed_and_resume_contains_real_output(self):
        self.command()
        self.client.stream("Сколько RAM?", self.events.append, self.cancelled)
        proposal = next(e for e in self.events if e["type"] == "command")
        self.assertEqual("free -h", proposal["command"])
        self.assertTrue(self.events[-1]["awaiting_command"])
        self.answer("Доступно 16 GiB.")
        self.client.continue_after_command("free -h", "Exit status: 0\nMem: available 16Gi", self.events.append, self.cancelled)
        history = self.requests[1]["messages"]
        self.assertEqual("shell", history[-2]["tool_calls"][0]["function"]["name"])
        self.assertEqual("tool", history[-1]["role"])
        self.assertIn("16Gi", history[-1]["content"])
        self.assertFalse(self.events[-1]["awaiting_command"])

    def test_follow_up_keeps_question_tool_result_and_final_answer(self):
        self.command()
        self.client.stream("Сколько RAM?", self.events.append, self.cancelled)
        self.answer("Доступно 16 GiB.")
        self.client.continue_after_command("free -h", "Exit code: 0\nMem: available 16Gi",
                                           self.events.append, self.cancelled)
        # The UI cancels outstanding work before submitting every new question.
        self.client.cancel()
        self.answer("Да, 16 GiB хватит.")
        self.client.stream("Этого хватит для игры?", self.events.append, threading.Event())
        history = self.requests[-1]["messages"]
        self.assertEqual(["system", "user", "assistant", "tool", "assistant", "user"],
                         [message["role"] for message in history])
        self.assertEqual("Сколько RAM?", history[1]["content"])
        self.assertEqual("free -h", history[2]["tool_calls"][0]["function"]["arguments"]["command"])
        self.assertIn("16Gi", history[3]["content"])
        self.assertEqual("Доступно 16 GiB.", history[4]["content"])
        self.assertEqual("Этого хватит для игры?", history[5]["content"])

    def test_follow_up_keeps_regular_conversation(self):
        self.answer("Привет, Миша.")
        self.client.stream("Меня зовут Миша", self.events.append, self.cancelled)
        self.client.cancel()
        self.answer("Миша.")
        self.client.stream("Как меня зовут?", self.events.append, threading.Event())
        history = self.requests[-1]["messages"]
        self.assertEqual(["system", "user", "assistant", "user"],
                         [message["role"] for message in history])
        self.assertEqual("Меня зовут Миша", history[1]["content"])
        self.assertEqual("Привет, Миша.", history[2]["content"])

    def test_cancelled_proposal_has_a_tool_result_before_follow_up(self):
        self.command()
        self.client.stream("Проверь память", self.events.append, self.cancelled)
        self.client.cancel()
        self.client.cancel()  # Repeated UI cancellation must not duplicate tool results.
        self.answer("Хорошо.")
        self.client.stream("Не надо, просто поговорим", self.events.append, threading.Event())
        history = self.requests[-1]["messages"]
        self.assertEqual(["system", "user", "assistant", "tool", "user"],
                         [message["role"] for message in history])
        self.assertEqual("shell", history[3]["tool_name"])
        self.assertIn("прерван", history[3]["content"])
        self.client.continue_after_command("free -h", "obsolete", self.events.append, threading.Event())
        self.assertEqual("error", self.events[-1]["type"])
        self.assertEqual(2, len(self.requests))

    def test_follow_up_keeps_real_tool_output_after_model_error(self):
        self.command("df -h")
        self.client.stream("Сколько места?", self.events.append, self.cancelled)
        self.responses.append((500, [{"error": "temporary failure"}]))
        self.client.continue_after_command("df -h", "Exit code: 0\n/dev/test 100G 25G 75G",
                                           self.events.append, self.cancelled)
        self.assertEqual("error", self.events[-1]["type"])
        self.client.cancel()
        self.answer("Свободно 75G.")
        self.client.stream("Объясни результат", self.events.append, threading.Event())
        history = self.requests[-1]["messages"]
        self.assertEqual(["system", "user", "assistant", "tool", "user"],
                         [message["role"] for message in history])
        self.assertEqual("Exit code: 0\n/dev/test 100G 25G 75G", history[-2]["content"])

    def test_follow_up_keeps_tool_output_when_interpretation_is_interrupted(self):
        self.command("pwd")
        self.client.stream("Где я?", self.events.append, self.cancelled)
        self.answer("Ты находишься", " в домашней папке.")

        def interrupt(event):
            self.events.append(event)
            if event["type"] == "text":
                self.cancelled.set()
                self.client.cancel()

        self.client.continue_after_command("pwd", "Exit code: 0\n/home/test", interrupt, self.cancelled)
        self.answer("/home/test.")
        self.client.stream("Повтори путь", self.events.append, threading.Event())
        history = self.requests[-1]["messages"]
        self.assertEqual(["system", "user", "assistant", "tool", "user"],
                         [message["role"] for message in history])
        self.assertEqual("Exit code: 0\n/home/test", history[-2]["content"])

    def test_rejects_mismatched_or_already_consumed_confirmation(self):
        self.command()
        self.client.stream("RAM", self.events.append, self.cancelled)
        self.client.continue_after_command("rm -rf /", "", self.events.append, self.cancelled)
        self.assertEqual("error", self.events[-1]["type"])
        self.assertEqual(1, len(self.requests))
        self.answer("Ответ")
        self.client.continue_after_command("free -h", "ok", self.events.append, self.cancelled)
        self.client.continue_after_command("free -h", "ok", self.events.append, self.cancelled)
        self.assertEqual("error", self.events[-1]["type"])
        self.assertEqual(2, len(self.requests))

    def test_cancelled_prompt_never_starts_request(self):
        self.cancelled.set()
        self.client.stream("привет", self.events.append, self.cancelled)
        self.assertEqual([], self.requests)
        self.assertEqual([], self.events)

    def test_new_prompt_invalidates_previous_proposal(self):
        self.command()
        self.client.stream("RAM", self.events.append, self.cancelled)
        self.answer("Привет")
        self.client.stream("привет", self.events.append, self.cancelled)
        self.client.continue_after_command("free -h", "ok", self.events.append, self.cancelled)
        self.assertEqual("error", self.events[-1]["type"])
        self.assertEqual(2, len(self.requests))

    def test_model_missing_has_actionable_error(self):
        self.responses.append((404, [{"error": "model not found"}]))
        self.client.stream("Hi", self.events.append, self.cancelled)
        self.assertEqual("error", self.events[-1]["type"])
        self.assertIn("настройки ИИ", self.events[-1]["message"])

    def test_malformed_or_empty_commands_are_not_offered(self):
        for command in ["", "\0bad", "x" * 4097, None]:
            with self.subTest(command=repr(command)[:30]):
                self.command(command=command)
                self.events.clear()
                self.client.stream("Run something", self.events.append, self.cancelled)
                self.assertEqual("error", self.events[-1]["type"])
                self.assertFalse(any(e["type"] == "command" for e in self.events))

    def test_repeated_tool_loop_uses_latest_output(self):
        self.command("pwd")
        self.client.stream("Где я и какая дата?", self.events.append, self.cancelled)
        self.command("date -I")
        self.client.continue_after_command("pwd", "/home/test", self.events.append, self.cancelled)
        self.answer("Домашняя папка, сегодня 28 октября.")
        self.client.continue_after_command("date -I", "2026-10-28", self.events.append, self.cancelled)
        tools = [m for m in self.requests[-1]["messages"] if m["role"] == "tool"]
        self.assertEqual(["/home/test", "2026-10-28"], [m["content"] for m in tools])

    def test_invalid_api_schema_is_an_error_event_instead_of_worker_crash(self):
        malformed = [
            [], None, {"message": None}, {"message": {"content": 42}},
            {"message": {"tool_calls": None}},
            {"message": {"tool_calls": [None]}, "done": True},
            {"message": {"tool_calls": [{"function": None}]}, "done": True},
        ]
        for chunk in malformed:
            with self.subTest(chunk=chunk):
                self.responses.append((200, [chunk]))
                self.events.clear()
                self.client.stream("Hi", self.events.append, self.cancelled)
                self.assertEqual("error", self.events[-1]["type"])


class OpenAiClientTest(HTTPTest):
    api_format = "openai"

    def answer(self, text="Ответ"):
        self.responses.append((200, [
            {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}, "[DONE]",
        ]))

    def command(self):
        self.responses.append((200, [
            {"choices": [{"delta": {"role": "assistant", "content": None, "tool_calls": [
                {"index": 0, "id": "call-1", "type": "function", "function": {
                    "name": "sh", "arguments": '{"command":"free',
                }},
            ]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
                "name": "ell", "arguments": ' -h","explanation":"Память"}',
            }}]}, "finish_reason": "tool_calls"}]}, "[DONE]",
        ]))

    def test_stream_authentication_payload_and_usage(self):
        self.client.api_key = "private-api-key"
        self.answer("Привет!")
        self.client.stream("Hi", self.events.append, self.cancelled)
        self.assertEqual(self.paths, ["/chat/completions"])
        self.assertEqual(self.request_headers[0]["Authorization"], "Bearer private-api-key")
        self.assertFalse(any(key in self.requests[0] for key in ("options", "think", "keep_alive")))
        self.assertFalse(self.requests[0]["parallel_tool_calls"])
        self.assertEqual(self.events[-1]["stats"]["eval_count"], 2)
        self.assertEqual(self.events[0], {"type": "text", "text": "Привет!"})
        self.assertNotIn("private-api-key", json.dumps(self.events))

    def test_fragmented_tool_call_and_real_result_replay(self):
        self.command()
        self.client.stream("RAM?", self.events.append, self.cancelled)
        self.assertEqual(self.events[-2]["command"], "free -h")
        self.answer()
        self.client.continue_after_command("free -h", "Exit status: 0\n16Gi", self.events.append, self.cancelled)
        assistant, result = self.requests[-1]["messages"][-2:]
        self.assertEqual(assistant["tool_calls"][0]["id"], "call-1")
        self.assertEqual(json.loads(assistant["tool_calls"][0]["function"]["arguments"])["command"], "free -h")
        self.assertEqual(result, {"role": "tool", "tool_call_id": "call-1", "content": "Exit status: 0\n16Gi"})

    def test_cancelled_tool_call_is_closed_before_follow_up(self):
        self.command()
        self.client.stream("RAM?", self.events.append, self.cancelled)
        self.client.cancel()
        self.answer()
        self.client.stream("Отмена", self.events.append, self.cancelled)
        result = self.requests[-1]["messages"][-2]
        self.assertEqual(result["tool_call_id"], "call-1")
        self.assertIn("прерван", result["content"])

    def test_truncated_tool_stream_never_offers_command(self):
        self.responses.append((200, [{"choices": [{"delta": {"tool_calls": [{"index": 0,
            "id": "call-1", "function": {"name": "shell", "arguments": '{"command":"pwd"}'},
        }]}}]}]))
        self.client.stream("Hi", self.events.append, self.cancelled)
        self.assertEqual(self.events[-1]["type"], "error")
        self.assertFalse(any(item["type"] == "command" for item in self.events))

    def test_http_errors_never_echo_response_or_token(self):
        self.client.api_key = "private-api-key"
        self.responses.append((401, [{"error": "private-api-key"}]))
        self.client.stream("Hi", self.events.append, self.cancelled)
        self.assertIn("ключ", self.events[-1]["message"])
        self.assertNotIn("private-api-key", json.dumps(self.events))

    def test_connection_check_only_lists_models(self):
        self.client.api_key = "private-api-key"
        self.assertIn("модель доступна", self.client.check_connection())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.paths, ["/models"])
        self.assertEqual(self.request_headers[0]["Authorization"], "Bearer private-api-key")

    def test_redirect_cannot_forward_key_to_another_origin(self):
        self.client.api_key = "private-api-key"
        self.redirect_to = f"http://localhost:{self.server.server_port}/other-api"
        self.assertIn("HTTP 302", self.client.check_connection())
        self.assertEqual(self.paths, ["/models"])

    def test_malformed_events_report_errors(self):
        for part in (None, {}, {"choices": None}, {"choices": [None]},
                     {"choices": [{"delta": {"tool_calls": [None]}}]}):
            with self.subTest(part=part):
                self.responses.append((200, [part]))
                self.events.clear()
                self.client.stream("Hi", self.events.append, self.cancelled)
                self.assertEqual(self.events[-1]["type"], "error")


if __name__ == "__main__":
    unittest.main()
