import os
import re
import atexit
from fastmcp import FastMCP

from graph import GraphDB
from vector import VectorDB
from watcher import VaultWatcher

# Setup FastMCP server
mcp = FastMCP("Second Brain")

# Setup project directories for database storage
project_dir = os.path.dirname(os.path.abspath(__file__))
db_dir = os.path.join(project_dir, ".db_data")
os.makedirs(db_dir, exist_ok=True)

kuzu_dir = os.path.join(db_dir, "kuzu")
chroma_dir = os.path.join(db_dir, "chroma")

# Initialize DB wrappers
graph_db = GraphDB(kuzu_dir)
vector_db = VectorDB(chroma_dir)

# Read vault path from environment variable or default to local directory
vault_path = os.getenv("VAULT_PATH")
if not vault_path:
    print("[Server] Warning: VAULT_PATH environment variable is not set. Defaulting to local './vault' folder.")
    vault_path = os.path.abspath("./vault")
    os.makedirs(vault_path, exist_ok=True)
else:
    vault_path = os.path.abspath(vault_path)

# Initialize and start vault file watcher
watcher = VaultWatcher(vault_path, graph_db, vector_db)
watcher.initial_sync()
watcher.start()

# Ensure background watchdog threads are stopped clean on server exit
atexit.register(watcher.stop)


@mcp.tool()
def search_notes(query: str) -> list[dict]:
    """
    Performs semantic search across all note contents in the vault using ChromaDB.
    Returns the top-5 most relevant unique notes.
    """
    return vector_db.search_notes(query, top_n=5)


@mcp.tool()
def list_all_notes() -> list[dict]:
    """
    Returns all notes stored in the vault, ordered by last-modified date (newest first).
    Each entry contains: title, tags, and last_modified timestamp.
    """
    return graph_db.list_all_notes()


@mcp.tool()
def get_note(title: str) -> dict:
    """
    Retrieves the full content, tags, outgoing links, and incoming backlinks for a specific note from the graph.
    """
    note = graph_db.get_note(title)
    if not note:
        return {"error": f"Note '{title}' not found in second brain."}
    return note


@mcp.tool()
def find_related(title: str) -> list[str]:
    """
    Finds 1-hop and 2-hop related notes for a specific note title using graph traversal.
    """
    return graph_db.find_related(title)


@mcp.tool()
def list_tags() -> list[dict]:
    """
    Returns all unique tags used across the vault, each with a note count.
    Results are sorted by usage count (most-used tags first).
    """
    return graph_db.list_tags()


@mcp.tool()
def search_by_tag(tag: str) -> list[dict]:
    """
    Returns all notes that carry the specified tag (case-insensitive).
    Each result includes the note title, its full tag list, and last_modified date.
    """
    return graph_db.search_by_tag(tag)


@mcp.tool()
def surface_contradictions(topic: str) -> list[dict]:
    """
    Finds notes with conflicting statements on a given topic using local semantic search and NLI/lexical checks.
    """
    # 1. Search for topic relevant chunks
    search_results = vector_db.search_notes(topic, top_n=10)
    if not search_results:
        return []

    # 2. Split chunks into sentences
    def split_sentences(text: str) -> list[str]:
        sentences = re.split(r'(?<=[.!?])\s+', text)
        return [s.strip() for s in sentences if len(s.strip()) > 12]

    sentences_by_note = {}
    for res in search_results:
        note_title = res["title"]
        content = res["content"]
        sentences = split_sentences(content)
        if sentences:
            if note_title not in sentences_by_note:
                sentences_by_note[note_title] = []
            sentences_by_note[note_title].extend(sentences)

    note_titles = list(sentences_by_note.keys())
    if len(note_titles) < 2:
        return []

    contradictions = []

    # Try to load local NLI cross-encoder
    tokenizer = None
    model = None
    try:
        import torch
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        model_name = "cross-encoder/nli-MiniLM2-L6-H768"
        
        global _nli_tokenizer, _nli_model
        if '_nli_tokenizer' not in globals():
            print("[Contradiction] Loading NLI model locally...")
            _nli_tokenizer = AutoTokenizer.from_pretrained(model_name)
            _nli_model = AutoModelForSequenceClassification.from_pretrained(model_name)
            
        tokenizer = _nli_tokenizer
        model = _nli_model
        
        label2id = model.config.label2id
        contra_idx = None
        for lbl, idx in label2id.items():
            if "contradict" in lbl.lower():
                contra_idx = idx
                break
        if contra_idx is None:
            contra_idx = 0
    except Exception as e:
        print(f"[Contradiction] Could not load NLI model (will use Jaccard negation fallback): {e}")

    # Compare sentences from different notes
    for i in range(len(note_titles)):
        for j in range(i + 1, len(note_titles)):
            title_a = note_titles[i]
            title_b = note_titles[j]
            
            for s_a in sentences_by_note[title_a]:
                for s_b in sentences_by_note[title_b]:
                    if tokenizer and model:
                        try:
                            inputs = tokenizer(s_a, s_b, return_tensors="pt", truncation=True)
                            with torch.no_grad():
                                logits = model(**inputs).logits
                            probs = torch.softmax(logits, dim=1)[0]
                            contra_prob = float(probs[contra_idx])
                            
                            if contra_prob > 0.7:  # Contradiction threshold
                                contradictions.append({
                                    "note_a": title_a,
                                    "note_b": title_b,
                                    "statement_a": s_a,
                                    "statement_b": s_b,
                                    "confidence": contra_prob,
                                    "method": "NLI Model"
                                })
                        except Exception:
                            pass
                    else:
                        # Fallback Lexical Jaccard Negation Heuristic
                        words_a = set(re.findall(r'\b\w+\b', s_a.lower()))
                        words_b = set(re.findall(r'\b\w+\b', s_b.lower()))
                        
                        stop_words = {"the", "a", "an", "is", "are", "was", "were", "to", "for", "in", "on", "at", "by", "with", "this", "that", "it"}
                        words_a = words_a - stop_words
                        words_b = words_b - stop_words
                        
                        if not words_a or not words_b:
                            continue
                            
                        intersection = words_a.intersection(words_b)
                        union = words_a.union(words_b)
                        jaccard = len(intersection) / len(union)
                        
                        # High semantic/lexical overlap in subjects
                        if jaccard > 0.4:
                            negations = {"not", "never", "no", "fails", "refutes", "contradicts", "doesn't", "don't", "won't", "isn't", "aren't"}
                            has_neg_a = any(w in negations for w in words_a)
                            has_neg_b = any(w in negations for w in words_b)
                            
                            if has_neg_a != has_neg_b:
                                contradictions.append({
                                    "note_a": title_a,
                                    "note_b": title_b,
                                    "statement_a": s_a,
                                    "statement_b": s_b,
                                    "confidence": 0.8,
                                    "method": "Lexical Negation Fallback"
                                })

    # Sort results by confidence descending
    contradictions = sorted(contradictions, key=lambda x: x['confidence'], reverse=True)
    return contradictions


@mcp.tool()
def daily_context(date: str) -> list[dict]:
    """
    Returns notes tagged with that date or modified on that day.
    Input date format: YYYY-MM-DD.
    """
    return graph_db.get_daily_context(date)


if __name__ == "__main__":
    mcp.run()
