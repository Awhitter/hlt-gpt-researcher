import unittest
from unittest.mock import MagicMock, patch, AsyncMock
import asyncio
from gpt_researcher.agent import GPTResearcher
from gpt_researcher.actions.query_processing import get_search_results
from types import SimpleNamespace
import os

class TestQuickSearch(unittest.TestCase):

    @patch('gpt_researcher.agent.get_search_results', new_callable=AsyncMock)
    @patch('gpt_researcher.agent.create_chat_completion', new_callable=AsyncMock)
    @patch('langchain_openai.OpenAIEmbeddings')
    def test_quick_search_no_summary(self, mock_embeddings, mock_create_chat, mock_search):
        # Setup mocks
        mock_search.return_value = [{'title': 'Test Result', 'content': 'Content', 'url': 'http://test.com'}]

        # Initialize researcher with dummy config to avoid API key issues
        researcher = GPTResearcher(query="test query")

        # Run quick_search without summary
        results = asyncio.run(researcher.quick_search("test query", aggregated_summary=False))

        # Verify
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['title'], 'Test Result')
        mock_create_chat.assert_not_called()

    @patch('gpt_researcher.agent.get_search_results', new_callable=AsyncMock)
    @patch('gpt_researcher.agent.create_chat_completion', new_callable=AsyncMock)
    @patch('langchain_openai.OpenAIEmbeddings')
    def test_quick_search_with_summary(self, mock_embeddings, mock_create_chat, mock_search):
        # Setup mocks
        mock_search.return_value = [{'title': 'Test Result', 'content': 'Content', 'url': 'http://test.com'}]
        mock_create_chat.return_value = "This is a summary."

        # Initialize researcher
        researcher = GPTResearcher(query="test query")

        # Run quick_search with summary
        summary = asyncio.run(researcher.quick_search("test query", aggregated_summary=True))

        # Verify
        self.assertEqual(summary, "This is a summary.")
        mock_create_chat.assert_called_once()

class TestQuickSearchLimits(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.retriever = MagicMock(__name__='TestRetriever')
        self.rows = [
            {'title': f'Source {i}', 'body': f'Evidence {i}', 'href': f'https://example.org/{i}'}
            for i in range(5)
        ]
        self.rows[1] = {'title': 'Source 1', 'raw_content': 'Evidence 1', 'url': 'https://example.org/1'}
        self.rows[2] = {'title': 'Source 2', 'content': 'Evidence 2', 'url': 'https://example.org/2'}
        self.retriever.return_value.search.return_value = self.rows
        self.researcher = object.__new__(GPTResearcher)
        self.researcher.retrievers = [self.retriever]
        self.researcher.cfg = SimpleNamespace(
            max_search_results_per_query=3, smart_llm_model='test',
            smart_llm_provider='test', smart_token_limit=100, llm_kwargs={},
        )
        self.researcher.prompt_family = MagicMock()

    async def test_web_quick_search_caps_an_overreturning_retriever(self):
        results = await self.researcher.quick_search('question', query_domains=['example.org'])
        self.retriever.assert_called_once_with('question', query_domains=['example.org'])
        self.retriever.return_value.search.assert_called_once_with(max_results=3)
        self.assertEqual(results, self.rows[:3])

    @patch('gpt_researcher.agent.create_chat_completion', new_callable=AsyncMock)
    async def test_summary_receives_only_bounded_source_text_and_urls(self, complete):
        complete.return_value = 'summary'
        await self.researcher.quick_search('question', aggregated_summary=True)
        _, context = self.researcher.prompt_family.generate_quick_summary_prompt.call_args.args
        for i in range(3):
            self.assertIn(f'Evidence {i}', context)
            self.assertIn(f'https://example.org/{i}', context)
        self.assertNotIn('Source 3', context)
        self.assertNotIn('Source 4', context)

    async def test_unbounded_helper_keeps_the_retriever_default(self):
        self.assertEqual(await get_search_results('question', self.retriever), self.rows)
        self.retriever.return_value.search.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
