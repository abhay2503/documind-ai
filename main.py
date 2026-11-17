import sys

from docling.chunking import HybridChunker
from docling.document_converter import DocumentConverter
from dotenv import load_dotenv
from groq import Groq
from transformers import AutoTokenizer

load_dotenv()  # reads GROQ_API_KEY from the .env file

pdf_path = sys.argv[1]


# 1. PDF -> Docling -> DoclingDocument
doc = DocumentConverter().convert(pdf_path).document


# 2. DoclingDocument -> HybridChunker -> chunks
tokenizer = AutoTokenizer.from_pretrained("sentence-transformers/all-MiniLM-L6-v2")
chunker = HybridChunker(tokenizer=tokenizer, max_tokens=512, merge_peers=True)

chunks = []
for chunk in chunker.chunk(dl_doc=doc):
    chunks.append(chunker.serialize(chunk=chunk))  # heading + text

print("Number of chunks:", len(chunks))
print("First chunk:\n", chunks[0])


# 3. chunks -> LLM -> summary
text = "\n\n".join(chunks)

client = Groq()
response = client.chat.completions.create(
    model="openai/gpt-oss-20b",
    messages=[{"role": "user", "content": "Summarize this document:\n\n" + text}],
)

print("\nSUMMARY:\n", response.choices[0].message.content)
