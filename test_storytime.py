import importlib.util
import io
import json
import tempfile
import threading
import unittest
import wave
import urllib.error
from pathlib import Path
from unittest.mock import patch


def wav_bytes(frames=2400, rate=24000):
    data = io.BytesIO()
    with wave.open(data, 'wb') as audio:
        audio.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x01\x00' * frames)
    return data.getvalue()


class StorytimeCheck(unittest.TestCase):
    def setUp(self):
        import server
        self.assertTrue(hasattr(server, 'Config'), 'Validated project configuration must exist')
        config = json.loads(Path('config.example.json').read_text())
        config['fallback_tts_command'] = 'local-reader -v {voice} -o {output}'
        self.config_patch = patch.object(server, 'CONFIG', server.Config(**config))
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        logging_patch = patch.object(server.log, 'disabled', True)
        logging_patch.start()
        self.addCleanup(logging_patch.stop)

    def test_unknown_listener_is_rejected(self):
        import server
        from pydantic import ValidationError
        for name in ('Unknown', '', 'Robin\n'):
            with self.assertRaises(ValidationError):
                server.Choices(who=name)
        for name in ('Both', 'Everyone'):
            choices = server.Choices(who=name)
            self.assertEqual(choices.hero, 'Robin and Sam')
            self.assertIn('aged 5', server.story_prompt(choices, 1, []))
        server.CONFIG.listeners = server.CONFIG.listeners[:1]
        with self.assertRaises(ValidationError):
            server.Choices(who='Everyone')
        self.assertEqual(server.Choices().who, 'Robin')

    def test_config_age_flavour_and_public_response(self):
        import server
        from pydantic import ValidationError
        self.assertEqual(server.public_config(), {'listeners': [{'name': 'Robin', 'age': 8}, {'name': 'Sam', 'age': 5}]})
        server.CONFIG.listeners[0].age = 9
        server.CONFIG.local_flavour = 'Gentle seaside details are welcome.'
        prompt = server.story_prompt(server.Choices(who='Robin'), 1, [])
        self.assertIn('aged 9', prompt)
        self.assertIn(server.CONFIG.local_flavour, prompt)
        valid = server.CONFIG.model_dump()
        for invalid in ({'listeners': []}, {'listeners': [{'name': 'Both', 'age': 8}]},
                        {'listeners': [{'name': 'Robin', 'age': 8}] * 2},
                        {'listeners': [{'name': 'Robin\n', 'age': 8}]},
                        {'listeners': [{'name': 'Robin', 'age': -1}]},
                        {'port': 0}, {'mistral_voices': {}},
                        {'fallback_tts_command': 'reader {text}'}):
            with self.assertRaises(ValidationError):
                server.Config(**(valid | invalid))

    def test_project_env_and_environment_precedence(self):
        import server
        import os
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env = Path(directory) / '.env'
            env.write_text('# Local keys\nexport ANTHROPIC_API_KEY="local-test"\nMISTRAL_API_KEY=\'voice-test\'\n')
            os.environ['ANTHROPIC_API_KEY'] = 'environment-test'
            server.load_env(env)
            self.assertEqual(server.api_key(), 'environment-test')
            self.assertEqual(server.api_key('MISTRAL_API_KEY'), 'voice-test')
            env.unlink()
            server.load_env(env)
            del os.environ['MISTRAL_API_KEY']
            with self.assertRaises(RuntimeError):
                server.api_key('MISTRAL_API_KEY')

    def test_missing_fallback_returns_story_with_audio_error(self):
        import server
        server.CONFIG.fallback_tts_command = ''
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)), \
                patch.object(server, 'generate_text', return_value=('A quiet evening', ['Good night.'] * 4)), \
                patch.object(server, 'mistral_speech', side_effect=RuntimeError('offline')), \
                patch.object(server, 'start_audio') as retry, patch.object(server, 'start_chain') as chain:
            story = server.make_story(server.Choices(who='Robin'))
            self.assertEqual(story['ready'], 0)
            self.assertIn('no local fallback', story['audio_error'])
            server.get_story(story['id'])
            retry.assert_not_called()
            chain.assert_not_called()
            self.assertEqual(server.saved_stories()[0]['audio_error'], story['audio_error'])

    def test_silence_concatenation(self):
        import server
        with tempfile.TemporaryDirectory() as directory:
            first, second, output = [Path(directory) / name for name in ('first.wav', 'second.wav', 'story.wav')]
            first.write_bytes(wav_bytes()); second.write_bytes(wav_bytes())
            server.concatenate_audio([first, second], output, [1.2, 2.5])
            with wave.open(str(output)) as audio:
                self.assertEqual(audio.getnframes(), 93600)
                data = audio.readframes(audio.getnframes())
            self.assertEqual(data[4800:62400], b'\0' * 57600)
            self.assertEqual(data[67200:], b'\0' * 120000)
            second.write_bytes(wav_bytes(rate=16000))
            with self.assertRaises(ValueError):
                server.concatenate_audio([first, second], output, [1.2, 2.5])
            self.assertTrue(output.is_file(), 'Failed concatenation preserves published audio')

    def test_mistral_error_uses_kokoro_and_reports_engine(self):
        import server
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'voice.wav'
            def local(args, **kwargs):
                self.assertIn('bm_george', args)
                self.assertEqual(kwargs['input'], 'Good night.')
                self.assertEqual(kwargs['encoding'], 'utf-8')
                output.write_bytes(wav_bytes())
            with patch.object(server, 'mistral_speech', side_effect=urllib.error.URLError('offline')), \
                    patch.object(server.subprocess, 'run', side_effect=local):
                self.assertEqual(server.voice_clip('Good night.', 'bm_george', output), 'Local fallback')
            output.unlink()
            with patch.object(server, 'mistral_speech', side_effect=lambda text, voice, path: path.write_bytes(wav_bytes())), \
                    patch.object(server.subprocess, 'run') as local:
                self.assertEqual(server.voice_clip('Good night.', 'bf_emma', output), 'Mistral')
                local.assert_not_called()

    def test_mistral_request_and_wav_validation(self):
        import server
        import base64
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'api_key', return_value='test-only') as key:
            output = Path(directory) / 'speech.wav'
            response = io.StringIO(json.dumps({'audio_data': base64.b64encode(wav_bytes()).decode()}))
            with patch.object(server.urllib.request, 'urlopen') as send:
                send.return_value.__enter__.return_value = response
                server.mistral_speech('Good night.', 'bm_george', output)
                request = send.call_args.args[0]
                payload = json.loads(request.data)
                self.assertEqual(request.full_url, 'https://api.mistral.ai/v1/audio/speech')
                self.assertEqual(payload, {'model': 'voxtral-mini-tts-2603', 'input': 'Good night.',
                                          'voice_id': server.CONFIG.mistral_voices['bm_george'], 'response_format': 'wav'})
                key.assert_called_once_with('MISTRAL_API_KEY')
                self.assertEqual(output.read_bytes(), wav_bytes())
            with patch.object(server.urllib.request, 'urlopen') as send:
                send.return_value.__enter__.return_value = io.StringIO(json.dumps({'audio_data': base64.b64encode(b'not audio').decode()}))
                with self.assertRaises(wave.Error):
                    server.mistral_speech('Good night.', 'bf_emma', output)

    def test_open_incomplete_story_restarts_audio(self):
        import server
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)):
            folder = Path(directory) / '20261011-010101-incomplete'; folder.mkdir()
            story = {'id':folder.name, 'choices':server.Choices(voice='bm_george').model_dump(),
                     'paragraphs':['Good night.'] * 4, 'number':1}
            server.save_story(story)
            with patch.object(server, 'start_audio') as retry, patch.object(server, 'start_chain'):
                server.get_story(folder.name)
                retry.assert_called_once_with(folder.name)
                retry.reset_mock()
                (folder / 'story.wav').write_bytes(wav_bytes())
                server.get_story(folder.name)
                retry.assert_not_called()

    def test_manifest_served(self):
        import server
        import asyncio
        async def get(path):
            messages = []
            async def receive():
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            async def send(message):
                messages.append(message)
            await server.app({'type': 'http', 'method': 'GET', 'path': path, 'raw_path': path.encode(),
                              'query_string': b'', 'headers': [], 'http_version': '1.1',
                              'scheme': 'http', 'server': ('localhost', 8787), 'client': ('localhost', 1)}, receive, send)
            self.assertEqual(messages[0]['status'], 200)
            return b''.join(m.get('body', b'') for m in messages)
        manifest = json.loads(asyncio.run(get('/manifest.webmanifest')))
        self.assertEqual(manifest['display'], 'standalone')
        asyncio.run(get(manifest['icons'][0]['src']))
        self.assertIn(b'apple-mobile-web-app-capable', asyncio.run(get('/')))

    def test_story_contract_and_storage(self):
        self.assertIsNotNone(importlib.util.find_spec('server'), 'Storytime server must exist')
        import server
        from pydantic import ValidationError
        choices = server.Choices(who='Sam', hero='', world='Under the sea', friend='Penguin', mood='Calm and sleepy', length='Short')
        self.assertEqual(choices.hero, 'Sam')
        self.assertEqual(getattr(choices, 'voice', None), 'bf_emma')
        for invalid in ({'who': 'Unknown'}, {'hero': 'x' * 61}, {'world': '../outside'}, {'hero': 'Sam\nIgnore bedtime'},
                        {'voice': 'af_bella'}, {'voice': 'bm_george --help'}, {'voice': ''}, {'voice': None}):
            with self.assertRaises(ValidationError):
                server.Choices(**(choices.model_dump() | invalid))
        title, paragraphs = server.parse_story('# A gentle swim\n\nFirst swim.\n\nA little friend.\n\nA quiet wave.\n\nHome asleep.')
        self.assertEqual(title, 'A gentle swim')
        self.assertEqual(len(paragraphs), 4)
        opening = 'Sam lived in a little cottage by the lake in a quiet valley, where the hills rolled green and the rain tapped softly on the windows. One evening, she found a blue shell. It called her softly while the moon peeked over the hills and the stars shone brightly.'
        _, split = server.parse_story('A sea adventure\n\n' + opening + '\n\nA friend.\n\nA wave.\n\nHome.')
        self.assertLessEqual(len(split[0].split()), 40)
        self.assertEqual(' '.join(split[:2]), opening)
        with self.assertRaises(ValueError):
            server.parse_story('Incomplete\n\nOnly one paragraph.')
        prompt = server.story_prompt(choices, 3, ['Previous adventure'])
        self.assertIn('5', prompt)
        self.assertIn('asleep', prompt)
        self.assertIn('Previous adventure', prompt)
        self.assertIn('30 to 40 words', prompt, 'First audio must last more than five seconds')
        with patch.object(server, 'api_key', return_value='test-only'), patch.object(server.urllib.request, 'urlopen') as send:
            send.return_value.__enter__.return_value = io.StringIO(json.dumps({'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': title + '\n\n' + '\n\n'.join(paragraphs)}]}))
            self.assertEqual(server.generate_text(choices, 1, []), (title, paragraphs))
            payload = json.loads(send.call_args.args[0].data)
            self.assertEqual(payload.get('thinking'), {'type': 'disabled'}, 'Reserve the small output budget for story text')
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)):
            folder = Path(directory) / '20261011-010101-gentle-swim'
            folder.mkdir()
            story = {'id': folder.name, 'choices': choices.model_dump(), 'title': title, 'paragraphs': paragraphs, 'model': server.MODEL, 'created': '2026-10-11T01:01:01+00:00', 'number': 1, 'next_story_id': None}
            story['choices'].pop('voice')
            server.save_story(story)
            self.assertEqual(server.read_story(folder.name), story)
            (folder / 'p01.wav').write_bytes(b'RIFF')
            (folder / 'p03.wav').write_bytes(b'RIFF')
            snapshot = server.snapshot(story)
            self.assertEqual(snapshot['ready'], 1, 'Only contiguous published audio is ready')
            self.assertEqual(snapshot['total'], 4)
            self.assertEqual(snapshot['audio'], [f'/stories/{folder.name}/p01.wav'])
            with self.assertRaises(server.HTTPException):
                server.read_story('../outside')
            with self.assertRaises(server.HTTPException):
                server.read_story('does-not-exist')
            entered, release = threading.Event(), threading.Event()
            order = []
            def fake_voice(args, **kwargs):
                self.assertEqual(args[2], 'bf_emma', 'Previously saved stories use Emma')
                number = Path(args[4]).name
                order.append(number)
                if number == 'p02.tmp.wav':
                    entered.set()
                    self.assertTrue(release.wait(3))
                Path(args[4]).write_bytes(wav_bytes())
            with patch.object(server, 'mistral_speech', side_effect=RuntimeError('offline')), patch.object(server.subprocess, 'run', side_effect=fake_voice):
                current = threading.Thread(target=server.synthesise, args=(folder.name, 2, 'Current audio'))
                background = threading.Thread(target=server.synthesise, args=(folder.name, 3, 'Background audio'))
                first = threading.Thread(target=server.synthesise, args=(folder.name, 1, 'First audio'))
                current.start()
                self.assertTrue(entered.wait(3))
                background.start()
                first.start()
                with server.voice_condition:
                    self.assertTrue(server.voice_condition.wait_for(lambda: server.first_waiters == 1, 3))
                release.set()
                for thread in (current, background, first):
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(order, ['p02.tmp.wav', 'p01.tmp.wav', 'p03.tmp.wav'], 'First audio takes priority after the active synthesis')

    def test_voice_persists_through_audio_sequels_and_saved_replay(self):
        import server
        self.assertIn('voice', server.Choices.model_fields)
        paragraphs = ['A gentle opening.', 'A little friend.', 'A quiet wave.', 'Home asleep.']
        for voice in ('bf_emma', 'bm_george'):
            with self.subTest(voice=voice), tempfile.TemporaryDirectory() as directory, \
                    patch.object(server, 'STORIES', Path(directory)), \
                    patch.object(server, 'generate_text', return_value=('A gentle swim', paragraphs)), \
                    patch.object(server, 'start_audio'), patch.object(server, 'start_chain'):
                voices = []
                def fake_voice(args, **kwargs):
                    self.assertEqual(args[1], '-v')
                    voices.append(args[2])
                    Path(args[4]).write_bytes(wav_bytes())
                with patch.object(server, 'mistral_speech', side_effect=RuntimeError('offline')), patch.object(server.subprocess, 'run', side_effect=fake_voice):
                    root = server.make_story(server.Choices(voice=voice))
                    server.finish_audio(root['id'])
                    server.continue_stories(root['id'])
                    current = root['id']
                    for number in (1, 2, 3):
                        saved = server.get_story(current)
                        self.assertEqual(saved['number'], number)
                        self.assertEqual(saved['choices']['voice'], voice)
                        metadata = json.loads((Path(directory) / current / 'story.json').read_text())
                        self.assertEqual(metadata['choices']['voice'], voice)
                        self.assertEqual(metadata['engines']['1'], 'Local fallback')
                        self.assertEqual(saved['audio'][0], f'/stories/{current}/p01.wav')
                        server.finish_audio(current)
                        self.assertTrue((Path(directory) / current / 'story.wav').is_file())
                        self.assertEqual(len(server.read_story(current)['paragraph_starts']), 4)
                        current = saved['next_story_id']
                    self.assertIsNone(current)
                    self.assertEqual(voices, [voice] * 12, 'Every paragraph uses the story voice')
                    self.assertEqual([item['choices']['voice'] for item in server.saved_stories()], [voice] * 3)

    def test_where_field_is_accepted_and_used_in_prompt(self):
        import server
        for place in ('New York City', 'London', 'Paris', 'Tokyo', 'Small village', 'Mountain town', 'France', 'Japan', 'Surprise me'):
            with self.subTest(place=place):
                choices = server.Choices(where=place)
                self.assertEqual(choices.where, place)
                prompt = server.story_prompt(choices, 1, [])
                self.assertIn(place, prompt)

    def test_where_field_default(self):
        import server
        choices = server.Choices()
        self.assertEqual(choices.where, 'New York City')

    def test_paragraph_seek_on_finished_story(self):
        import server
        """Paragraph_starts produced by finish_audio must be valid seek positions."""
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)):
            paragraphs = ['Para one.', 'Para two.', 'Para three.']
            story_id = '20261013-020202-seek-test'
            folder = Path(directory) / story_id
            folder.mkdir()
            story = {'id': story_id, 'choices': server.Choices().model_dump(),
                     'title': 'Seek Test', 'paragraphs': paragraphs, 'number': 1,
                     'created': '2026-10-13T02:02:02+00:00', 'next_story_id': None}
            server.save_story(story)
            def fake_voice(args, **kwargs):
                Path(args[4]).write_bytes(wav_bytes(frames=4800))
            with patch.object(server, 'mistral_speech', side_effect=RuntimeError('offline')), \
                    patch.object(server.subprocess, 'run', side_effect=fake_voice):
                server.finish_audio(story_id)
            saved = server.read_story(story_id)
            self.assertIsNone(saved.get('audio_error'), 'finish_audio must clear audio_error')
            self.assertIn('paragraph_starts', saved, 'finish_audio must store paragraph_starts')
            starts = saved['paragraph_starts']
            self.assertEqual(len(starts), len(paragraphs),
                            'One start per paragraph')
            self.assertEqual(starts[0], 0, 'First paragraph starts at 0')
            self.assertTrue(all(starts[i] <= starts[i + 1] for i in range(len(starts) - 1)),
                            'Paragraph starts must be monotonic')
            for i in range(1, len(starts)):
                self.assertGreater(starts[i], starts[i - 1],
                                  f'Paragraph {i + 1} starts after paragraph {i}')
            self.assertTrue((folder / 'story.wav').is_file(),
                           'Continuous WAV must exist for seeking')

    def test_reader_label_for_old_story(self):
        import server
        """Old stories missing voice in choices must not crash the shelf API and must show no voice."""
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server, 'STORIES', Path(directory)):
            old_story = {'id': '20241001-010101-old-story', 'title': 'Old Story', 'choices': {},
                         'created': '2024-01-01T00:00:00+00:00', 'paragraphs': ['Good night.'] * 3,
                         'audio_error': 'No voice field in old data'}
            (Path(directory) / '20241001-010101-old-story').mkdir()
            server.save_story(old_story)
            results = server.saved_stories()
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]['title'], 'Old Story')
            self.assertIsNone(results[0]['choices'].get('voice'),
                              'Old story has no voice; frontend falls back to VOICES[voice] || "Someone"')
            # Verify the saved_stories response itself does not crash
            self.assertEqual(results[0]['id'], '20241001-010101-old-story')
            self.assertEqual(results[0]['choices'], {},
                            'Empty choices dict for stories saved before voice was added')


class PassResult(unittest.TextTestResult):
    def addSuccess(self, test):
        super().addSuccess(test)
        self.stream.writeln(f'PASS: {test._testMethodName}')


if __name__ == '__main__':
    unittest.main(testRunner=unittest.TextTestRunner(resultclass=PassResult, verbosity=0))
