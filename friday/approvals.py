"""Asking the owner for permission, and taking the answer from wherever it comes first.

Voice, typed text, the dashboard's Approve/Deny buttons, a phone notification button (one-time token) or a text
message (YES <PIN>). Split out of main.py so the approval path can be read and tested on its own."""
from __future__ import annotations

import hmac
import queue
import secrets
import threading
import time
import uuid

from .voice import is_clear_answer, is_yes

PTT_SIGNAL = "\x00PTT"


class ApprovalsMixin:
    """Mixed into Assistant: every method runs with `self` = the Assistant instance."""

    def confirm(self, prompt: str) -> bool:
        """One question at a time: the chat and every background project may ask, and the answers must not get mixed up."""
        lock = self.__dict__.get("_confirm_lock") or self.__dict__.setdefault("_confirm_lock", threading.RLock())
        with lock:
            return self._confirm_one(prompt)

    def _confirm_one(self, prompt: str) -> bool:
        """Ask for permission before dangerous actions / credit use: voice, typed, the dashboard's Approve/Deny
        buttons, or by text message (YES <PIN>) when SMS is set up. The first answer wins."""
        from .credits import wait_seconds

        cid = uuid.uuid4().hex[:8]
        self.bus.pending_confirm = {"id": cid, "prompt": prompt}
        self.bus.publish("confirm", {"pending": self.bus.pending_confirm})
        token = secrets.token_urlsafe(16)  # one-time key that lets the phone notification's Approve/Deny button answer this one request
        self._approval_tokens = {cid: token}
        try:
            from . import notify

            notify.push("Jarvis needs your OK", prompt[:400], text_too=False,
                        data={"approval": {"id": cid, "token": token, "prompt": prompt[:400]}})
        except Exception as e:
            self.bus.log_event("notify", f"approval notification failed: {e}", level="error")
        while not self._confirm_answers.empty():  # drop stale clicks
            self._confirm_answers.get_nowait()
        answered: list[bool] = []
        from_sms = getattr(self._tls, "via", "") == "sms"
        use_sms = self.sms.configured()
        wait = max(15, wait_seconds())
        if use_sms:
            def by_text():
                try:
                    ok = self.sms.ask_approval(prompt, wait)
                except Exception as e:
                    self.bus.log_event("sms", f"approval text failed: {e}", level="error")
                    ok = None
                if ok is not None:
                    self._confirm_answers.put((cid, ok))

            threading.Thread(target=by_text, daemon=True).start()

        def dash_answer() -> bool:
            try:
                got_id, ok = self._confirm_answers.get_nowait()
                if got_id == cid:
                    answered.append(ok)
            except queue.Empty:
                pass
            return bool(answered)

        try:
            answer = ""
            if from_sms:  # the request came by text: don't grab the microphone, answer by text or dashboard
                self.ui.info("Waiting for approval by text or on the dashboard: " + prompt)
            else:
                self.say(prompt + (" Say yes or no." if self.recorder else " Type yes or no, or use the dashboard."))
                if self.recorder and not self.bus.flags.get("mic_muted"):
                    self.ui.info("(say 'yes' / 'no', type it, click Approve/Deny on the dashboard"
                                 + (", or reply to the text)" if use_sms else ")"))
                    answer = self.listen_command(wait=8, abort=dash_answer)
                    if answer and not is_clear_answer(answer):  # room noise / other talk is not a "no": keep waiting
                        self.ui.info(f"(heard '{answer[:40]}', which isn't a yes or no - still waiting)")
                        answer = ""
            deadline = time.time() + wait
            while not answer and not answered and time.time() < deadline:
                if dash_answer():
                    break
                if from_sms:
                    time.sleep(0.25)
                    continue
                try:
                    answer = self.inputs.get(timeout=0.25)
                    if answer == PTT_SIGNAL:
                        answer = ""
                    answer = answer.replace("\x00DASH", "")
                    if answer and not is_clear_answer(answer):  # typed/spoken something else: not an answer
                        answer = ""
                except queue.Empty:
                    pass
            if answered:
                ok = answered[0]
                self.ui.user("(approved)" if ok else "(denied)", via="dashboard/text")
            else:
                self.ui.user(answer or "(no answer)")
                ok = is_yes(answer)
            self.ui.info("Confirmed." if ok else "Not confirmed - cancelled.")
            return ok
        finally:
            self.bus.pending_confirm = None
            self.bus.publish("confirm", {"pending": None})
            pending = self.sms.pending
            if pending is not None and pending.get("answer") is None:
                pending["event"].set()  # answered elsewhere: stop waiting for the text reply

    def api_approval(self) -> dict | None:
        """The request waiting for a yes/no right now (for the phone page), with the key its buttons need."""
        p = self.bus.pending_confirm
        if not p:
            return None
        return {"id": p["id"], "prompt": p["prompt"], "token": getattr(self, "_approval_tokens", {}).get(p["id"], "")}

    def api_approve(self, cid: str, token: str, approve: bool) -> bool:
        """Answer from a phone notification button. Only works with the one-time token sent in that notification."""
        want = getattr(self, "_approval_tokens", {}).get(cid, "")
        if not want or not hmac.compare_digest(str(token or ""), want):
            return False
        ok = self.answer_confirm(cid, bool(approve))
        if ok:
            self._approval_tokens.pop(cid, None)  # single use
            self.bus.log_event("approval", f"{'Approved' if approve else 'Denied'} from the phone notification")
        return ok

    def answer_confirm(self, cid: str, approve: bool) -> bool:
        if not self.bus.pending_confirm or self.bus.pending_confirm["id"] != cid:
            return False
        self._confirm_answers.put((cid, approve))
        return True
