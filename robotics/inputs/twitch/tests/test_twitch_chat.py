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


if __name__ == "__main__":
    unittest.main()
