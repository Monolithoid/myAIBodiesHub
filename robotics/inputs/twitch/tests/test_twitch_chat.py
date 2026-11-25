"""Tests sans réseau des règles du tchat Twitch.

    cd robotics/inputs/twitch && python3 -m unittest discover -s tests -v
"""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import twitch_chat  # noqa: E402

LINE = ("@badges=subscriber/12;display-name=Joa\\sM;mod=0 "
        ":joa!joa@joa.tmi.twitch.tv PRIVMSG #monolithoid :!nao salut, ça va ?\r\n")


def msg(text, user="joa", t=0.0, mod="0"):
    return twitch_chat.ChatMessage(user=user, display_name=user, channel="c",
                                   text=text, tags={"mod": mod}, received_at=t)


class TestParse(unittest.TestCase):

    def test_privmsg_with_tags(self):
        m = twitch_chat.parse_privmsg(LINE, now=1.0)
        self.assertEqual(m.user, "joa")
        self.assertEqual(m.display_name, "Joa M")
        self.assertEqual(m.channel, "monolithoid")
        self.assertEqual(m.text, "!nao salut, ça va ?")
        self.assertFalse(m.is_mod)

    def test_other_lines_ignored(self):
        self.assertIsNone(twitch_chat.parse_privmsg("PING :tmi.twitch.tv"))
        self.assertIsNone(twitch_chat.parse_privmsg(":tmi.twitch.tv 001 justinfan1 :Welcome"))

    def test_broadcaster_is_mod(self):
        line = LINE.replace("badges=subscriber/12", "badges=broadcaster/1")
        self.assertTrue(twitch_chat.parse_privmsg(line).is_mod)


class TestCommand(unittest.TestCase):

    def test_extract(self):
        ex = twitch_chat.extract_command
        self.assertEqual(ex("!nao salut"), "salut")
        self.assertEqual(ex("!NAO, ça va ?"), "ça va ?")
        self.assertEqual(ex("\x01ACTION !nao danse\x01"), "danse")
        self.assertIsNone(ex("!nao"))
        self.assertIsNone(ex("!naomi salut"))
        self.assertIsNone(ex("salut !nao"))


class TestFilter(unittest.TestCase):

    def test_accepts_every_message_without_delay(self):
        f = twitch_chat.ChatFilter()
        self.assertEqual(f.check(msg("!nao salut"), now=0), ("salut", "ok"))
        self.assertEqual(f.check(msg("!nao encore"), now=0.5), ("encore", "ok"))

    def test_links_bots_mods(self):
        f = twitch_chat.ChatFilter(ignored_users=["Nightbot"])
        self.assertEqual(f.check(msg("!nao va sur www.site.com"))[1], "lien")
        self.assertEqual(f.check(msg("!nao regarde https://x.y"))[1], "lien")
        self.assertEqual(f.check(msg("!nao salut", user="nightbot"))[1], "utilisateur ignoré")
        mods = twitch_chat.ChatFilter(mods_only=True)
        self.assertEqual(mods.check(msg("!nao salut"))[1], "réservé aux modérateurs")
        self.assertEqual(mods.check(msg("!nao salut", mod="1"))[0], "salut")

    def test_truncate(self):
        f = twitch_chat.ChatFilter(max_chars=12)
        q, _ = f.check(msg("!nao un deux trois quatre"))
        self.assertEqual(q, "un deux…")


class TestQueue(unittest.TestCase):

    def test_burst_keeps_only_latest(self):
        q = twitch_chat.LatestQueue(max_age_s=90)
        self.assertIsNone(q.push(msg("!nao 0", t=0), "0"))
        for i in range(1, 4):
            replaced = q.push(msg(f"!nao {i}", t=0), str(i))
            self.assertEqual(replaced[1], str(i - 1))
        self.assertEqual(len(q), 1)
        self.assertEqual(q.pop(timeout=0, now=1)[1], "3")
        self.assertIsNone(q.pop(timeout=0, now=1))

    def test_pop_waits_for_next_message(self):
        q = twitch_chat.LatestQueue(max_age_s=90)
        threading.Timer(0.05, q.push, args=(msg("!nao tard"), "tard")).start()
        self.assertEqual(q.pop(timeout=2, now=0)[1], "tard")

    def test_old_messages_dropped(self):
        q = twitch_chat.LatestQueue(max_age_s=60)
        q.push(msg("!nao vieux", t=0), "vieux")
        self.assertIsNone(q.pop(timeout=0, now=100))

    def test_prompt(self):
        self.assertEqual(twitch_chat.brain_prompt(msg("x", user="Joa"), "salut"),
                         "[tchat Twitch] Joa : salut")


class TestViewerCommands(unittest.TestCase):

    def test_commands(self):
        self.assertEqual(twitch_chat.viewer_command("!rgpd"), "rgpd")
        self.assertEqual(twitch_chat.viewer_command(" !RESET "), "reset")
        self.assertEqual(twitch_chat.viewer_command("!stop"), "stop")
        for text in ("!reset tout", "!rgpdx", "!nao reset", "reset", "!stop le jeu"):
            self.assertIsNone(twitch_chat.viewer_command(text), text)

    def test_roles_from_badges(self):
        def roles(tags):
            return twitch_chat.parse_privmsg(
                f"@{tags} :bob!bob@bob.tmi.twitch.tv PRIVMSG #chan :!nao salut").roles
        self.assertEqual(roles("badges=;mod=0"), [])
        self.assertEqual(roles("badges=broadcaster/1;mod=0"), ["broadcaster", "moderator"])
        self.assertEqual(roles("badges=moderator/1;mod=1"), ["moderator"])
        self.assertEqual(roles("badges=vip/1,subscriber/12"), ["vip", "subscriber"])
        self.assertEqual(roles("badges=founder/0"), ["subscriber"])
        self.assertEqual(roles("badges=;subscriber=1"), ["subscriber"])

    def test_user_id(self):
        msg = twitch_chat.parse_privmsg(
            "@display-name=Bob;user-id=1234 :bob!bob@bob.tmi.twitch.tv PRIVMSG #chan :!rgpd")
        self.assertEqual(msg.user_id, "1234")

    def test_rgpd_texts(self):
        self.assertEqual(len(twitch_chat.rgpd_texts({})), 1)
        self.assertIn("rien", twitch_chat.rgpd_texts({})[0])
        texts = twitch_chat.rgpd_texts({"profile": "Aime la danse.", "messages": [
            {"message": "tu danses ?", "answer": "Oui !"},
            {"message": "et le rock ?", "answer": ""},
        ]})
        self.assertEqual(len(texts), 3)
        self.assertIn("Aime la danse.", texts[0])
        self.assertIn("!reset", texts[0])
        self.assertIn("tu danses ?", texts[1])
        self.assertIn("Oui !", texts[1])
        self.assertIn("raté", texts[2])
        long = twitch_chat.rgpd_texts({"profile": "x" * 900, "messages": [
            {"message": "y", "answer": "z" * 900}]})
        self.assertTrue(all(len(t) <= twitch_chat.WHISPER_MAX_CHARS for t in long))


class TestDropped(unittest.TestCase):

    def test_replaced_and_expired_are_reported(self):
        dropped = []
        q = twitch_chat.LatestQueue(max_age_s=10, on_drop=lambda m, q: dropped.append(q))
        q.push(msg("!nao un", t=0.0), "un")
        q.push(msg("!nao deux", t=1.0), "deux")
        self.assertEqual(dropped, ["un"])
        self.assertIsNone(q.pop(timeout=0, now=100.0))
        self.assertEqual(dropped, ["un", "deux"])


class TestAnswerReady(unittest.TestCase):

    def test_next_message_at_start(self):
        self.assertFalse(twitch_chat.answer_ready({"speaking": True, "started": False}, "start"))
        self.assertTrue(twitch_chat.answer_ready({"speaking": True, "started": True}, "start"))
        self.assertTrue(twitch_chat.answer_ready({"speaking": False}, "start"))

    def test_next_message_at_end(self):
        self.assertFalse(twitch_chat.answer_ready({"speaking": True, "started": True}, "end"))
        self.assertTrue(twitch_chat.answer_ready({"speaking": False, "started": True}, "end"))

    def test_older_body_gateway_waits_for_the_end(self):
        self.assertFalse(twitch_chat.answer_ready({"speaking": True}, "start"))


if __name__ == "__main__":
    unittest.main()
