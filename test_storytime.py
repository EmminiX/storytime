import importlib.util
import io
import json
import tempfile
import threading
import unittest
import wave
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch


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
        self.assertEqual(server.public_config(), {'listeners': [{'name': 'Robin', 'age': 8}, {'name': 'Sam', 'age': 5, 'picture': 'girl'}],
                                                  'readers': ['bf_emma', 'bm_george', 'el_nana', 'el_grandad'],
                                                  'reader_names': {'bf_emma': 'Emma', 'bm_george': 'George', 'el_nana': 'Nana', 'el_grandad': 'Grandad'}})
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

    def test_listener_picture_is_optional_and_validated(self):
        import server
        from pydantic import ValidationError
        valid = server.Config(**{
            'listeners': [{'name': 'Robin', 'age': 8}, {'name': 'Sam', 'age': 5}],
            'mistral_voices': {'bf_emma': 'one', 'bm_george': 'two'},
        }).model_dump()
        for picture in ('girl', 'boy'):
            config = server.Config(**(valid | {'listeners': [{'name': 'Robin', 'age': 8, 'picture': picture}, {'name': 'Sam', 'age': 5}]}) )
            self.assertEqual([listener.picture for listener in config.listeners], [picture, None])
        config = server.Config(**(valid | {'listeners': [{'name': 'Robin', 'age': 8}, {'name': 'Sam', 'age': 5, 'picture': 'girl'}]}))
        self.assertIsNone(config.listeners[0].picture)
        self.assertEqual(server.public_config()['listeners'][0], {'name': 'Robin', 'age': 8})
        self.assertEqual(server.public_config()['listeners'][1], {'name': 'Sam', 'age': 5, 'picture': 'girl'})
        for invalid in ('neutral', 'cat', ''):
            with self.assertRaises(ValidationError):
                server.Config(**(valid | {'listeners': [{'name': 'Robin', 'age': 8, 'picture': invalid}, {'name': 'Sam', 'age': 5}]}))

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
                patch.object(server, 'generate_text', return_value=('A quiet evening', ['Good night.'] * 4, None, None)), \
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
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        response = client.get('/manifest.webmanifest')
        self.assertEqual(response.status_code, 200)
        manifest = response.json()
        self.assertEqual(manifest['display'], 'standalone')
        icon_response = client.get(manifest['icons'][0]['src'])
        self.assertEqual(icon_response.status_code, 200)
        index_response = client.get('/')
        self.assertEqual(index_response.status_code, 200)
        self.assertIn(b'apple-mobile-web-app-capable', index_response.content)

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
            self.assertEqual(server.generate_text(choices, 1, []), (title, paragraphs, None, None))
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
                    patch.object(server, 'generate_text', return_value=('A gentle swim', paragraphs, None, None)), \
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
        self.assertIsNone(choices.where, 'where should be None by default, not New York City')
        prompt = server.story_prompt(choices, 1, [])
        self.assertNotIn('New York City', prompt, 'Old stories without where should not get New York City in prompt')

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

    def test_own_place_field(self):
        """A child may type their own place; it must reach the prompt and be validated."""
        import server
        from pydantic import ValidationError
        choices = server.Choices(where="Grandma's garden")
        self.assertEqual(choices.where, "Grandma's garden")
        self.assertIn("Grandma's garden", server.story_prompt(choices, 1, []))
        self.assertEqual(server.Choices(where='  Tokyo  ').where, 'Tokyo', 'Listed places are trimmed')
        for invalid in ('', '   ', 'Two\nlines', 'x' * 61):
            with self.subTest(place=invalid):
                with self.assertRaises(ValidationError):
                    server.Choices(where=invalid)

    def test_prev_story_link(self):
        """Story 2 must expose the id of story 1 so Story back can return to it."""
        import server
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)):
            one, two = '20261014-000001-first-night', '20261014-000002-second-night'
            for story_id, number, next_id in ((one, 1, two), (two, 2, None)):
                folder = Path(directory) / story_id; folder.mkdir()
                server.save_story({'id': story_id, 'choices': server.Choices().model_dump(), 'title': story_id,
                                   'paragraphs': ['Good night.'] * 4, 'number': number, 'created': '2026-10-14T01:01:01+00:00',
                                   'next_story_id': next_id})
                (folder / 'p01.wav').write_bytes(wav_bytes())
            self.assertIsNone(server.snapshot(server.read_story(one))['prev_story_id'])
            self.assertEqual(server.snapshot(server.read_story(two))['prev_story_id'], one)
            listed = server.saved_stories()
            self.assertEqual([item['prev_story_id'] for item in listed], [one, None],
                             'saved_stories exposes prev_story_id')

    def test_classics_serve_prerendered_audio(self):
        """Classics ship pre-rendered MP3 audio; the server never calls a runtime voice for them."""
        import server
        from fastapi.testclient import TestClient
        tale = server.classics.CLASSICS[0]
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)):
            audio = Path(directory) / 'classics_audio'
            (audio / tale['id']).mkdir(parents=True)
            (audio / tale['id'] / 'story.mp3').write_bytes(b'fake mp3')
            (audio / 'manifest.json').write_text(json.dumps({tale['id']: {'starts': [0, 12.5], 'seconds': 300}}))
            with patch.object(server, 'CLASSICS_AUDIO', audio):
                server.materialise_classic(tale)
                client = TestClient(server.app)
                listed = client.get('/api/classics').json()
                mine = next(item for item in listed if item['id'] == tale['id'])
                self.assertEqual(mine['continuous'], f"/classics_audio/{tale['id']}/story.mp3")
                self.assertEqual(mine['paragraph_starts'], [0, 12.5])
                self.assertIsNone(mine['audio_error'])
                served = client.get(f"/classics_audio/{tale['id']}/story.mp3")
                self.assertEqual(served.status_code, 200)
                self.assertEqual(served.headers['content-type'], 'audio/mpeg')
                self.assertEqual(client.get(f"/classics_audio/{tale['id']}/p01.wav").status_code, 404,
                                 'Only the pre-rendered MP3 is served')
                self.assertEqual(client.get('/classics_audio/classic-../outside/story.mp3').status_code, 404)
                with patch.object(server, 'start_audio') as retry, patch.object(server, 'start_chain') as chain:
                    opened = client.get(f"/api/story/{tale['id']}").json()
                    retry.assert_not_called()
                    chain.assert_not_called()
                self.assertEqual(opened['continuous'], f"/classics_audio/{tale['id']}/story.mp3")

    def test_classic_without_prerendered_audio_still_reports(self):
        """A classic whose MP3 is missing keeps the old honest error, not a silent failure."""
        import server
        tale = server.classics.CLASSICS[0]
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)), \
                patch.object(server, 'CLASSICS_AUDIO', Path(directory) / 'classics_audio'):
            server.materialise_classic(tale)
            with patch.object(server, 'start_audio') as retry:
                server.get_story(tale['id'])
                retry.assert_called_once_with(tale['id'])

    def test_classics_exist_and_word_range(self):
        import classics
        self.assertEqual(len(classics.CLASSICS), 17, 'Seventeen classic tales must ship')
        ids = [tale['id'] for tale in classics.CLASSICS]
        titles = [tale['title'] for tale in classics.CLASSICS]
        self.assertEqual(len(set(ids)), 17, 'Classic tale ids must be unique')
        self.assertEqual(len(set(titles)), 17, 'Classic tale titles must be unique')
        for tale in classics.CLASSICS:
            self.assertTrue(tale['id'].startswith('classic-'), tale['id'])
            self.assertTrue(tale['source'].startswith('after '), tale['title'])
            self.assertGreater(len(tale['paragraphs']), 4, tale['title'])
            words = classics.word_count(tale)
            self.assertGreaterEqual(words, classics.MIN_WORDS, tale['title'])
            self.assertLessEqual(words, classics.MAX_WORDS, tale['title'])
            self.assertGreaterEqual(classics.estimated_minutes(tale), 6, tale['title'])
            self.assertLessEqual(classics.estimated_minutes(tale), 10, tale['title'])

    def test_classics_shelf_lists_seventeen(self):
        import server
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)):
            client = TestClient(server.app)
            response = client.get('/api/classics')
            self.assertEqual(response.status_code, 200)
            classics = response.json()
            self.assertEqual(len(classics), 17, 'The Classics shelf lists seventeen tales')
            titles = [item['title'] for item in classics]
            for item in classics:
                self.assertIn('source', item, item.get('title'))
                self.assertTrue(item['source'].startswith('after '), item.get('title'))
            self.assertIn('Puss in Boots', titles)
            self.assertIn('Pinocchio', titles)
            self.assertIn('Hansel and Gretel', titles)
            self.assertIn('Fionn and the Salmon of Knowledge', titles)

    def test_classics_excluded_from_saved_stories(self):
        import server
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)):
            for tale in server.classics.CLASSICS:
                server.materialise_classic(tale)
            self.assertEqual(server.saved_stories(), [], 'Classics stay out of the made-story shelf')

    def test_classic_ids_read_safely(self):
        import server
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)):
            server.materialise_classic(server.classics.CLASSICS[0])
            story = server.read_story('classic-puss-in-boots')
            self.assertEqual(story['title'], 'Puss in Boots')
            self.assertTrue(story['classic'])
            self.assertEqual(story['source'], 'after Charles Perrault')
            with self.assertRaises(server.HTTPException):
                server.read_story('classic-../outside')
            with self.assertRaises(server.HTTPException):
                server.read_story('../outside')

    def test_classic_audio_uses_local_voice_only(self):
        import server
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)):
            server.materialise_classic(server.classics.CLASSICS[0])
            def local(args, **kwargs):
                self.assertEqual(args[2], 'bf_emma', 'Classics are read by the chosen voice')
                Path(args[4]).write_bytes(wav_bytes())
            mistral = Mock()
            with patch.object(server, 'mistral_speech', mistral), \
                    patch.object(server.subprocess, 'run', side_effect=local):
                server.synthesise('classic-puss-in-boots', 1, 'Once upon a time.')
            mistral.assert_not_called()
            self.assertEqual(server.read_story('classic-puss-in-boots')['engines']['1'], 'Local fallback')

    def test_archive_story_moves_it_out_of_shelf(self):
        import server
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)):
            folder = Path(directory) / '20261014-010101-gentle-swim'; folder.mkdir()
            story = {'id': folder.name, 'choices': server.Choices(who='Robin').model_dump(),
                     'title': 'A gentle swim', 'paragraphs': ['Good night.'] * 4, 'number': 1, 'created': '2026-10-14T01:01:01+01:00', 'next_story_id': None}
            server.save_story(story)
            (folder / 'p01.wav').write_bytes(wav_bytes())
            self.assertEqual(len(client.get('/api/stories').json()), 1)
            r = client.post(f'/api/story/{folder.name}/archive')
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()['archived'])
            self.assertEqual(len(client.get('/api/stories').json()), 0, 'Archived hidden from shelf')
            self.assertEqual(len(client.get('/api/stories?archived=true').json()), 1, 'Archived shown in archive')
            r = client.post(f'/api/story/{folder.name}/archive')
            self.assertFalse(r.json()['archived'])
            self.assertEqual(len(client.get('/api/stories').json()), 1)

    def test_classic_cannot_be_archived(self):
        import server
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)):
            server.materialise_classic(server.classics.CLASSICS[0])
            r = client.post('/api/story/classic-puss-in-boots/archive')
            self.assertEqual(r.status_code, 400)

    def test_delete_only_from_archive(self):
        import server
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)):
            folder = Path(directory) / '20261014-010101-gentle-swim'; folder.mkdir()
            story = {'id': folder.name, 'choices': server.Choices().model_dump(),
                     'title': 'A gentle swim', 'paragraphs': ['Good night.'] * 4, 'number': 1, 'created': '2026-10-14T01:01:01+01:00', 'next_story_id': None}
            server.save_story(story)
            (folder / 'p01.wav').write_bytes(wav_bytes())
            (folder / 'story.wav').write_bytes(wav_bytes())
            r = client.delete(f'/api/story/{folder.name}')
            self.assertEqual(r.status_code, 400, 'Deleted without archiving first')
            client.post(f'/api/story/{folder.name}/archive')
            r = client.delete(f'/api/story/{folder.name}')
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()['deleted'])
            self.assertFalse(folder.is_dir(), 'Story folder removed')

    def test_daily_cap_enforced_on_server(self):
        import server
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)), \
                patch.object(server, 'generate_text', return_value=('A quiet evening', ['Good night.'] * 4, None, None)), \
                patch.object(server, 'start_audio'), patch.object(server, 'start_chain'):
            def fake_voice(args, **kwargs):
                Path(args[4]).write_bytes(wav_bytes())
            with patch.object(server, 'mistral_speech', side_effect=RuntimeError('offline')), \
                    patch.object(server.subprocess, 'run', side_effect=fake_voice):
                server.CONFIG.daily_story_cap = 10
                for day in range(10):
                    r = client.post('/api/story', json=server.Choices(who='Robin').model_dump())
                    self.assertEqual(r.status_code, 200, f'Story {day + 1} should be created')
                r = client.post('/api/story', json=server.Choices(who='Robin').model_dump())
                self.assertEqual(r.status_code, 429, '11th story refused')
                status = client.get('/api/daily_cap').json()
                self.assertEqual(status['remaining'], 0)
                self.assertEqual(status['cap'], 10)

    def test_daily_cap_default_and_config(self):
        import server
        cfg1 = server.Config(**({k: v for k, v in server.CONFIG.model_dump().items() if k != 'daily_story_cap'}))
        self.assertEqual(cfg1.daily_story_cap, 10)
        cfg2 = server.Config(**(server.CONFIG.model_dump() | {'daily_story_cap': 5}))
        self.assertEqual(cfg2.daily_story_cap, 5)

    def test_archive_and_delete_no_effect_on_classics_shelf(self):
        import server
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)):
            server.materialise_classic(server.classics.CLASSICS[0])
            classics = client.get('/api/classics').json()
            self.assertGreaterEqual(len(classics), 1)
            r = client.delete('/api/story/classic-puss-in-boots')
            self.assertEqual(r.status_code, 400)
            classics2 = client.get('/api/classics').json()
            self.assertGreaterEqual(len(classics2), 1, 'Classic remains after failed delete')

    def test_favorites_toggle_and_flags(self):
        import server
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        classic_id = server.classics.CLASSICS[0]['id']
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)):
            folder = Path(directory) / '20261014-010101-gentle-swim'; folder.mkdir()
            server.save_story({'id': folder.name, 'choices': server.Choices(who='Robin').model_dump(),
                               'title': 'A gentle swim', 'paragraphs': ['Good night.'] * 4, 'number': 1,
                               'created': '2026-10-14T01:01:01+01:00', 'next_story_id': None})
            (folder / 'p01.wav').write_bytes(wav_bytes())
            self.assertEqual(client.get('/api/favorites').json(), [])
            self.assertFalse(client.get('/api/stories').json()[0]['favorite'])
            self.assertFalse(any(c['favorite'] for c in client.get('/api/classics').json()))
            r = client.post(f'/api/favorites/{folder.name}')
            self.assertEqual(r.json(), {'id': folder.name, 'favorite': True})
            self.assertEqual(client.post(f'/api/favorites/{classic_id}').json()['favorite'], True)
            self.assertEqual(client.get('/api/favorites').json(), [folder.name, classic_id])
            self.assertTrue(client.get('/api/stories').json()[0]['favorite'])
            self.assertEqual([c['id'] for c in client.get('/api/classics').json() if c['favorite']], [classic_id])
            self.assertTrue(client.get(f'/api/story/{folder.name}').json()['favorite'])
            for workers in (server.audio_threads, server.chain_threads):  # the GET started both workers: own and finish them before the fixture goes
                worker = workers.pop(folder.name, None)
                self.assertIsNotNone(worker)
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
            self.assertEqual(client.post(f'/api/favorites/{folder.name}').json(), {'id': folder.name, 'favorite': False})
            self.assertEqual(client.get('/api/favorites').json(), [classic_id])
            self.assertFalse(client.get('/api/stories').json()[0]['favorite'])
            self.assertFalse((Path(directory) / 'favorites.json.tmp').exists())

    def test_favorites_reject_unknown_and_traversal_ids(self):
        import server
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)):
            for bad in ('20261014-010101-missing', 'classic-no-such-tale', '..', 'classic-..', '%2e%2e%2fsecret', '..%2f..%2fetc%2fpasswd'):
                with self.subTest(story_id=bad):
                    self.assertEqual(client.post(f'/api/favorites/{bad}').status_code, 404)
            self.assertFalse((Path(directory) / 'favorites.json').exists())

    def test_corrupt_favorites_file_does_not_break_the_shelves(self):
        import server
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        for junk in (b'\xff\xfe', b'{not json', b'null', b'{"a": 1}'):
            with self.subTest(junk=junk), tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)):
                (Path(directory) / 'favorites.json').write_bytes(junk)
                self.assertEqual(client.get('/api/favorites').json(), [])
                self.assertEqual(client.get('/api/stories').status_code, 200)
                classics = client.get('/api/classics')
                self.assertEqual(classics.status_code, 200)
                self.assertFalse(any(c['favorite'] for c in classics.json()))
                self.assertTrue(client.post(f"/api/favorites/{server.classics.CLASSICS[0]['id']}").json()['favorite'])

    def test_word_and_quiz_are_parsed_checked_and_stripped(self):
        import server
        story = 'A gentle swim\n\nThe otter was patient.\n\nA friend.\n\nA wave.\n\nHome asleep.'
        good = (story + '\n\n---\nWORD: patient = able to wait calmly without getting cross\n'
                'Q: Who was patient? | The otter | The owl | The fox\nQ: Where did they swim? | The lake | The sky | A cave\n'
                'Q: How did it end? | Asleep | Dancing | Shouting\n')
        text, word, quiz = server.split_extras(good)
        self.assertEqual(text, story)
        self.assertEqual(word, {'word': 'patient', 'meaning': 'able to wait calmly without getting cross'})
        self.assertEqual(quiz[0], {'q': 'Who was patient?', 'answers': ['The otter', 'The owl', 'The fox']})
        self.assertEqual(len(quiz), 3)
        self.assertEqual(server.parse_story(text)[1][-1], 'Home asleep.', 'The block never reaches the voice')
        # A word missing from the story, a question with two answers, duplicate answers: the bad parts drop out.
        bad = (story + '\n---\nWORD: dragon = a big lizard\nQ: Who? | The otter | The owl\n'
               'Q: Where did they swim? | Lake | lake | Sky\nQ: How did it end? | Asleep | Dancing | Shouting')
        self.assertEqual(server.split_extras(bad), (story, None, None))
        self.assertEqual(server.split_extras(story), (story, None, None))
        no_rule = story + '\nWORD: otter = a river animal\n'
        self.assertEqual(server.split_extras(no_rule)[:2], (story, {'word': 'otter', 'meaning': 'a river animal'}))
        # Review MAY-1276: a line after the block, or a malformed one, never reaches the voice.
        trailing = good + 'Sleep tight!\nQ: half a question\n'
        text, word, quiz = server.split_extras(trailing)
        self.assertEqual((text, word['word'], len(quiz)), (story, 'patient', 3))
        # A story paragraph that starts with Q: or WORD: stays when there is no --- block.
        for prefix in ('Q: what did the otter want?', 'WORD: that was the last word she said.'):
            told = story + '\n\n' + prefix
            self.assertEqual(server.split_extras(told), (told, None, None))
            self.assertEqual(len(server.parse_story(told)[1]), 5)
        # Round 2: a closing rule after the block, and a block with no rule followed by chatter.
        text, word, quiz = server.split_extras(good + '---\n')
        self.assertEqual((text, word['word'], len(quiz)), (story, 'patient', 3))
        bare = good.replace('\n\n---\n', '\n\n') + 'Sleep tight, little one!\n'
        text, word, quiz = server.split_extras(bare)
        self.assertEqual((text, word['word'], len(quiz)), (story, 'patient', 3))
        # Round 3: a rule inside the block, and a malformed first question with no rule: all of it goes.
        inner = story + '\n\n---\nWORD: patient = calm\n---\nQ: Who was patient? | The otter | The owl | The fox\nQ: Where did they swim? | Lake | Sky | Cave\nQ: How did it end? | Asleep | Dancing | Shouting'
        text, word, quiz = server.split_extras(inner)
        self.assertEqual((text, word['word'], len(quiz)), (story, 'patient', 3))
        broken = story + '\nWORD: patient = calm\nQ: Who? | The otter | The owl\nQ: Where? | Lake | Sky | Cave\nQ: End? | Asleep | Dancing | Shouting\n'
        self.assertEqual(server.split_extras(broken), (story, {'word': 'patient', 'meaning': 'calm'}, None))
        # Round 4: a stray line inside the block, with and without the opening rule.
        stray = good.replace('Q: Where did', 'A stray line\nQ: Where did')
        for case in (stray, stray.replace('\n\n---\n', '\n\n')):
            text, word, quiz = server.split_extras(case)
            self.assertEqual((text, word['word'], len(quiz)), (story, 'patient', 3))
        # Round 5: a narrative Q: or WORD: paragraph right before a block without a rule stays story;
        # a stray line with blank lines around it, inside a block without a rule, goes with the block.
        bare_block = good.replace('\n\n---\n', '\n\n')
        for prefix in ('Q: what did the otter want?', 'WORD: that was the last word she said.'):
            told = story + '\n\n' + prefix
            text, word, quiz = server.split_extras(bare_block.replace(story, told, 1))
            self.assertEqual((text, word['word'], len(quiz)), (told, 'patient', 3))
        spaced = bare_block.replace('Q: Where did', 'A stray line\n\nQ: Where did')
        alone = bare_block.replace('Q: Where did', '\nA stray line\n\nQ: Where did')  # the stray line is its own group
        for case in (spaced, alone):
            text, word, quiz = server.split_extras(case)
            self.assertEqual((text, word['word'], len(quiz)), (story, 'patient', 3))
        # Round 6: a WORD line, two malformed questions and one good one, in separate groups, all go.
        malformed = story + '\n\nWORD: patient = able to wait calmly\n\nQ: Who waited? | Otter | Owl\n\nQ: Where were they? | Lake | Sky\n\nQ: How did it end? | Asleep | Dancing | Shouting'
        self.assertEqual(server.split_extras(malformed), (story, {'word': 'patient', 'meaning': 'able to wait calmly'}, None))
        self.assertEqual(server.parse_story(server.split_extras(malformed)[0])[1][-1], 'Home asleep.', 'None of it is read aloud')
        # Round 7: a block in which no line is valid still never reaches the voice.
        hopeless = story + '\n\nWORD: patient = \nQ: Who waited? | Otter | Owl\nQ: Where were they? | Lake'
        self.assertEqual(server.split_extras(hopeless), (story, None, None))
        questions_only = story + '\n\nQ: Who waited? | Otter | Owl\n\nQ: Where were they? | Lake | Sky\n\nQ: How did it end? | Asleep | Dancing'
        self.assertEqual(server.split_extras(questions_only), (story, None, None))
        # A --- scene break with no WORD or Q lines after it is story, not a block.
        scene = 'A gentle swim\n\nThe otter swam.\n\n---\n\nA friend.\n\nA wave.\n\nHome asleep.'
        self.assertEqual(server.split_extras(scene), (scene, None, None))
        self.assertEqual(server.split_extras(scene + '\n\n---\nWORD: otter = a river animal')[:2], (scene, {'word': 'otter', 'meaning': 'a river animal'}))
        with patch.object(server, 'api_key', return_value='test-only'), patch.object(server.urllib.request, 'urlopen') as send:
            send.return_value.__enter__.return_value = io.StringIO(json.dumps({'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': good}]}))
            title, paragraphs, word, quiz = server.generate_text(server.Choices(), 1, [])
            self.assertEqual((title, paragraphs[-1], word['word'], len(quiz)), ('A gentle swim', 'Home asleep.', 'patient', 3))
            self.assertIn('WORD:', json.loads(send.call_args.args[0].data)['messages'][0]['content'])

    def test_sentences_split_keeps_quotes(self):
        import server
        self.assertEqual(server.sentences('The moon rose. "Goodnight, stars," he said. "Sleep well!" And the stars blinked? Yes.'),
                         ['The moon rose.', '"Goodnight, stars," he said.', '"Sleep well!"', 'And the stars blinked?', 'Yes.'])
        self.assertEqual(server.sentences("'Sleep well!' The owl slept."), ["'Sleep well!'", 'The owl slept.'])
        self.assertEqual(server.sentences('One line only'), ['One line only'])

    def test_mistral_reads_one_sentence_per_request_with_pauses(self):
        import server
        import base64
        sent = []
        def reply(request, timeout):
            sent.append(json.loads(request.data)['input'])
            response = Mock()
            response.__enter__ = Mock(return_value=io.StringIO(json.dumps({'audio_data': base64.b64encode(wav_bytes(100)).decode()})))
            response.__exit__ = Mock(return_value=False)
            return response
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'api_key', return_value='test-only'), \
                patch.object(server.urllib.request, 'urlopen', side_effect=reply):
            output = Path(directory) / 'speech.wav'
            server.mistral_speech('The moon rose. The fox slept.', 'bf_emma', output)
            self.assertEqual(sent, ['The moon rose.', 'The fox slept.'])
            with wave.open(str(output)) as audio:
                self.assertEqual(audio.getnframes(), 100 + round(24000 * server.CONFIG.sentence_pause) + 100)

    def test_mistral_blocked_sentence_retries_the_whole_paragraph(self):
        import server
        sent = []
        def frames(text, voice):
            sent.append(text)
            if text == 'He held on tighter.':
                raise urllib.error.HTTPError('u', 403, 'guardrail', {}, None)
            return b'\x01\x00' * 10
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'mistral_frames', side_effect=frames):
            output = Path(directory) / 'speech.wav'
            server.mistral_speech('The wind blew. He held on tighter.', 'bf_emma', output)
            self.assertEqual(sent, ['The wind blew.', 'He held on tighter.', 'The wind blew. He held on tighter.'])
            with wave.open(str(output)) as audio:
                self.assertEqual(audio.getnframes(), 10, 'The paragraph reading replaces the sentence one')
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(server, 'mistral_frames', side_effect=urllib.error.HTTPError('u', 401, 'key', {}, None)), \
                self.assertRaises(urllib.error.HTTPError):
            server.mistral_speech('One. Two.', 'bf_emma', Path(directory) / 'speech.wav')

    def test_elevenlabs_request_and_twin_fallback(self):
        import server
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'api_key', return_value='test-only') as key, \
                patch.object(server, 'eleven_has_room', return_value=True):
            output = Path(directory) / 'speech.wav'
            with patch.object(server.urllib.request, 'urlopen') as send:
                send.return_value.__enter__.return_value = io.BytesIO(b'\x01\x00' * 2400)
                self.assertEqual(server.voice_clip('Good night.', 'el_nana', output, 'Before.'), 'ElevenLabs')
                request = send.call_args.args[0]
                self.assertEqual(request.full_url, f"https://api.elevenlabs.io/v1/text-to-speech/{server.CONFIG.elevenlabs_voices['el_nana']}?output_format=pcm_24000")
                payload = json.loads(request.data)
                self.assertEqual((payload['text'], payload['model_id'], payload['previous_text']), ('Good night.', 'eleven_multilingual_v2', 'Before.'))
                self.assertEqual(payload['voice_settings'], server.ELEVEN_SETTINGS)
                key.assert_called_with('ELEVENLABS_API_KEY')
                self.assertEqual(output.read_bytes(), wav_bytes())
            twin = []
            with patch.object(server.urllib.request, 'urlopen', side_effect=urllib.error.HTTPError('u', 401, 'no', {}, None)), \
                    patch.object(server, 'mistral_speech', side_effect=lambda text, voice, path: twin.append(voice)):
                self.assertEqual(server.voice_clip('Good night.', 'el_grandad', output), 'Mistral')
                self.assertEqual(twin, ['bm_george'])

    def test_elevenlabs_only_reads_story_one_inside_the_quota(self):
        import server
        def quota(used):
            response = Mock()
            response.__enter__ = Mock(return_value=io.StringIO(json.dumps({'character_limit': 33136, 'character_count': used})))
            response.__exit__ = Mock(return_value=False)
            return response
        with patch.object(server, 'api_key', return_value='test-only'):
            with patch.object(server.urllib.request, 'urlopen', return_value=quota(20000)):
                self.assertTrue(server.eleven_has_room(3136))
            with patch.object(server.urllib.request, 'urlopen', return_value=quota(20000)):
                self.assertFalse(server.eleven_has_room(3137), 'The 10,000 reserve stays untouched')
            with patch.object(server.urllib.request, 'urlopen', side_effect=urllib.error.URLError('offline')):
                self.assertFalse(server.eleven_has_room(1))
        nana = server.Choices(voice='el_nana')
        with patch.object(server, 'eleven_has_room', return_value=True) as room:
            self.assertEqual(server.story_reader(nana, 1, ['abc', 'de']), 'el_nana')
            room.assert_called_once_with(5)
            self.assertEqual(server.story_reader(nana, 2, ['abc']), 'bf_emma')
            self.assertEqual(server.story_reader(server.Choices(voice='el_grandad'), 3, ['abc']), 'bm_george')
            self.assertEqual(server.story_reader(server.Choices(voice='bm_george'), 1, ['abc']), 'bm_george')
        with patch.object(server, 'eleven_has_room', return_value=False):
            self.assertEqual(server.story_reader(nana, 1, ['abc']), 'bf_emma')
        server.CONFIG.elevenlabs_voices = {}
        with patch.object(server, 'eleven_has_room') as room:
            self.assertEqual(server.story_reader(nana, 1, ['abc']), 'bf_emma')
            room.assert_not_called()

    def eleven_run(self, paragraphs, counter_follows, fail_first=False, counter=None):
        """Read paragraphs as Nana with a mocked account: 20,000 of 33,136 used, so 3,136 above the reserve."""
        import server
        charged = []
        def reply(request, timeout):
            response = Mock()
            used = counter(len(charged)) if counter else 20000 + (sum(charged) if counter_follows else 0)
            response.__enter__ = Mock(return_value=io.StringIO(json.dumps({'character_limit': 33136, 'character_count': used})))
            response.__exit__ = Mock(return_value=False)
            return response
        def speak(text, voice, path, previous=''):
            charged.append(len(text))  # the provider charges as soon as it answers
            if fail_first and len(charged) == 1:
                raise OSError('disk full while saving')
            path.write_bytes(wav_bytes())
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'api_key', return_value='test-only'), \
                patch.object(server.urllib.request, 'urlopen', side_effect=reply), \
                patch.object(server, 'eleven_booked', {'last': None, 'outstanding': 0, 'at': float('-inf')}), \
                patch.object(server, 'eleven_speech', side_effect=speak), \
                patch.object(server, 'mistral_speech', side_effect=lambda text, voice, path: path.write_bytes(wav_bytes())):
            engines = [server.voice_clip(text, 'el_nana', Path(directory) / f'{n}.wav') for n, text in enumerate(paragraphs)]
        return engines, sum(charged)

    def test_overlapping_stories_never_spend_the_reserve(self):
        # Two Nana stories of two 1,000-character paragraphs each, interleaved, while the account counter lags.
        engines, charged = self.eleven_run(['x' * 1000] * 4, counter_follows=False)
        self.assertEqual(engines, ['ElevenLabs'] * 3 + ['Mistral'])
        self.assertLessEqual(charged, 3136, 'The 10,000 reserve stays untouched')
        # A request that was charged but then failed locally stays booked.
        engines, charged = self.eleven_run(['x' * 1000] * 4, counter_follows=False, fail_first=True)
        self.assertEqual(engines, ['Mistral', 'ElevenLabs', 'ElevenLabs', 'Mistral'])
        self.assertLessEqual(charged, 3136)
        # A counter that keeps up is not counted twice: a story that fits is read to the end.
        engines, charged = self.eleven_run(['x' * 750] * 4, counter_follows=True)
        self.assertEqual(engines, ['ElevenLabs'] * 4)
        self.assertEqual(charged, 3000)
        # Round 3: another app's 1,000 characters land with the first paragraph, then the counter lags.
        # That extra rise is never credit: the third paragraph would take the account below the reserve.
        engines, charged = self.eleven_run(['x' * 1000] * 3, counter_follows=False, counter=lambda done: 20000 if done == 0 else 22000)
        self.assertEqual(engines, ['ElevenLabs', 'ElevenLabs', 'Mistral'])
        self.assertLessEqual(charged + 1000, 3136, 'StoryTime plus the other app stay above the reserve')

    def test_late_quota_reading_cannot_move_the_baseline_back(self):
        import server
        readings = iter([20000, 20750, 20750, 20000, 20000, 20000])
        def reply(request, timeout):
            response = Mock()
            response.__enter__ = Mock(return_value=io.StringIO(json.dumps({'character_limit': 33136, 'character_count': next(readings)})))
            response.__exit__ = Mock(return_value=False)
            return response
        with patch.object(server, 'api_key', return_value='test-only'), patch.object(server.urllib.request, 'urlopen', side_effect=reply), \
                patch.object(server, 'eleven_booked', {'last': None, 'outstanding': 0, 'at': float('-inf')}):
            self.assertTrue(server.eleven_has_room(750, book=True))
            self.assertTrue(server.eleven_has_room(750, book=True))  # 20,750 settles the first booking
            self.assertTrue(server.eleven_has_room(750, book=True))
            self.assertEqual(server.eleven_booked['outstanding'], 1500)
            self.assertTrue(server.eleven_has_room(1))  # a late, lower reading settles nothing and keeps the baseline
            self.assertEqual((server.eleven_booked['last'], server.eleven_booked['outstanding']), (20750, 1500))
            # ...and frees nothing: 33,136 - 20,750 - 1,500 booked - 10,000 reserve = 886 left, not 1,636.
            self.assertFalse(server.eleven_has_room(887))
            self.assertTrue(server.eleven_has_room(886))
        # The reading happens under the lock, so two checks cannot interleave their readings.
        held = []
        def watched(*args, **kwargs):
            held.append(server.eleven_lock.locked())
            return reply(*args, **kwargs)
        with patch.object(server.urllib.request, 'urlopen', side_effect=watched), \
                patch.object(server, 'api_key', return_value='test-only'), patch.object(server, 'eleven_booked', {'last': None, 'outstanding': 0, 'at': float('-inf')}):
            readings = iter([20000])
            self.assertTrue(server.eleven_has_room(1))
        self.assertEqual(held, [True])

    def test_reader_slots_names_and_voice_ids_are_validated(self):
        import server
        from pydantic import ValidationError
        for voice in ('el_nana', 'el_grandad'):
            self.assertEqual(server.Choices(voice=voice).voice, voice)
        for bad in ('el_bad', 'nana'):
            with self.assertRaises(ValidationError):
                server.Choices(voice=bad)
        base = server.CONFIG.model_dump()
        renamed = server.Config(**(base | {'reader_names': {'el_nana': 'Granny'}}))
        self.assertEqual(renamed.reader_names, {'bf_emma': 'Emma', 'bm_george': 'George', 'el_nana': 'Granny', 'el_grandad': 'Grandad'})
        for invalid in ({'elevenlabs_voices': {'el_nana': '../v1/user'}}, {'elevenlabs_voices': {'el_bad': 'abcdefgh12'}},
                        {'reader_names': {'el_nana': ''}}, {'reader_names': {'el_nana': 'Nana\nignore'}}, {'elevenlabs_reserve_chars': -1}):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                server.Config(**(base | invalid))
        server.CONFIG.elevenlabs_voices = {'el_nana': 'abcdefgh12'}
        self.assertEqual(server.public_config()['readers'], ['bf_emma', 'bm_george', 'el_nana'])
        self.assertEqual(server.public_config()['reader_names']['el_grandad'], 'Grandad', 'Recorded classics still name their reader')

    def test_story_records_its_reader_word_and_quiz(self):
        import server
        quiz = [{'q': 'Who swam?', 'answers': ['Robin', 'Sam', 'Owl']}] * 3
        word = {'word': 'gentle', 'meaning': 'soft and kind'}
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)), \
                patch.object(server, 'generate_text', return_value=('A gentle swim', ['Good night.'] * 4, word, quiz)), \
                patch.object(server, 'eleven_has_room', return_value=False), \
                patch.object(server, 'voice_clip', side_effect=lambda text, voice, path, previous='': path.write_bytes(wav_bytes()) and 'Mistral') as clip, \
                patch.object(server, 'start_audio'), patch.object(server, 'start_chain'):
            story = server.make_story(server.Choices(voice='el_nana'))
            self.assertEqual((story['reader'], story['word'], story['quiz']), ('bf_emma', word, quiz))
            self.assertEqual(clip.call_args.args[1], 'bf_emma', 'Over the reserve, the Mistral twin reads')
            listed = server.saved_stories()[0]
            self.assertEqual((listed['reader'], listed['word'], listed['quiz']), ('bf_emma', word, quiz))

    def test_classics_carry_reader_word_and_quiz(self):
        import re
        import server
        for tale in server.classics.CLASSICS:
            with self.subTest(tale=tale['id']):
                self.assertIn(tale['reader'], ('bf_emma', 'bm_george', 'el_nana', 'el_grandad'))
                self.assertRegex(' '.join(tale['paragraphs']), re.compile(rf"\b{re.escape(tale['word']['word'])}\b", re.IGNORECASE))
                self.assertLessEqual(len(tale['word']['meaning'].split()), 12)
                self.assertEqual(len(tale['quiz']), 3)
                for question in tale['quiz']:
                    self.assertEqual(len({a.casefold() for a in question['answers']}), 3)
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'STORIES', Path(directory)):
            tale = server.classics.CLASSICS[0]
            folder = Path(directory) / tale['id']; folder.mkdir()
            old = server.classic_story(tale) | {'engines': {'1': 'Local fallback'}}
            del old['word'], old['quiz']
            (folder / 'story.json').write_text(json.dumps(old))
            server.materialise_classic(tale)
            stored = json.loads((folder / 'story.json').read_text())
            self.assertEqual((stored['word'], stored['engines']), (tale['word'], {'1': 'Local fallback'}), 'An old copy gains the new fields and keeps its own')

    def test_choose_paths_are_complete(self):
        import server
        from fastapi.testclient import TestClient
        paths = TestClient(server.app).get('/api/paths').json()
        self.assertEqual(len(paths), 2)
        for tale in paths:
            with self.subTest(tale=tale['id']):
                pages, seen, todo = tale['pages'], set(), [tale['start']]
                while todo:
                    key = todo.pop()
                    if key not in seen:
                        seen.add(key)
                        todo += [choice['to'] for choice in pages[key]['choices']]
                self.assertEqual(seen, set(pages), 'Every page is reachable and every choice leads somewhere')
                self.assertTrue(8 <= len(pages) <= 10)
                self.assertEqual(sum(not page['choices'] for page in pages.values()), 3)
                self.assertTrue(all(len(page['choices']) in (0, 2) for page in pages.values()))

    def test_static_route_serves_only_scripts_and_fonts(self):
        import server
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        response = client.get('/static/activities.js')
        self.assertEqual((response.status_code, response.headers['content-type'].split(';')[0]), (200, 'text/javascript'))
        fonts = sorted((server.STATIC / 'fonts').glob('*.woff2'))
        self.assertTrue(fonts, 'Lexend is self-hosted')
        self.assertEqual(client.get(f'/static/fonts/{fonts[0].name}').headers['content-type'], 'font/woff2')
        with tempfile.TemporaryDirectory() as directory:
            static, outside = Path(directory) / 'static', Path(directory) / 'outside.js'
            static.mkdir(); outside.write_text('secret'); (static / 'ok.js').write_text('ok')
            (static / 'link.js').symlink_to(outside)
            with patch.object(server, 'STATIC', static.resolve()):
                self.assertEqual(server.static_file('ok.js').path, (static / 'ok.js').resolve())
                for escape in ('../outside.js', 'link.js', str(outside)):
                    with self.subTest(escape=escape), self.assertRaises(server.HTTPException):
                        server.static_file(escape)
        for bad in ('/static/../server.py', '/static/%2e%2e/server.py', '/static/..%2fserver.py', '/static/index.html',
                    '/static/manifest.webmanifest', '/static/fonts/missing.woff2', '/static/%2e%2e/%2e%2e/etc/passwd'):
            with self.subTest(path=bad):
                self.assertEqual(client.get(bad).status_code, 404)

    def test_page_loads_nothing_from_outside_the_home(self):
        page = Path('static/index.html').read_text() + Path('static/activities.js').read_text()
        self.assertNotIn('googleapis', page)
        self.assertNotIn('gstatic', page)
        self.assertNotRegex(page, r'(?:src|href)="https?://')


class PassResult(unittest.TextTestResult):
    def addSuccess(self, test):
        super().addSuccess(test)
        self.stream.writeln(f'PASS: {test._testMethodName}')


if __name__ == '__main__':
    unittest.main(testRunner=unittest.TextTestRunner(resultclass=PassResult, verbosity=0))
