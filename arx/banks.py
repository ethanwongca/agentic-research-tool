"""Question banks: the decisions a researcher MUST make for each kind of stage.

Every stage of a given `kind` inherits these questions. Until each one has a
real answer (not "TBD", not "you decide"), the stage cannot be approved and the
agent cannot write code for it. Add your own kinds here.

`key` becomes a parameter passed to the stage's run(inputs, params), so the
researcher's answer drives the code instead of a value the agent made up.
"""

BANKS: dict[str, list[dict]] = {
    "data_processing": [
        {"key": "source", "question": "Where does the raw data come from (path/URL) and in what format?"},
        {"key": "record_unit", "question": "What is one record (row, document, sentence, ...)?"},
        {"key": "fields", "question": "Which fields are kept, and which one is the primary content?"},
        {"key": "cleaning", "question": "Exactly which cleaning/normalization steps (lowercase, strip HTML, unicode NFKC, ...)?"},
        {"key": "dedup", "question": "How are duplicates defined, and what happens to them?"},
        {"key": "missing", "question": "How are missing / empty / malformed records handled (drop, impute, fail loudly)?"},
        {"key": "filtering", "question": "Inclusion / exclusion criteria (length limits, language, date range, ...)?"},
        {"key": "sample", "question": "Full dataset or a sample? If a sample: size and seed."},
    ],
    "embedding": [
        {"key": "model", "question": "Which embedding model, exact name and version?"},
        {"key": "input_text", "question": "What exactly is embedded (which field, any prefix/prompt template)?"},
        {"key": "max_tokens", "question": "Max input length, and how is overflow handled (truncate head/tail, chunk + pool)?"},
        {"key": "normalize", "question": "L2-normalize the embeddings? (decides cosine vs euclidean downstream)"},
    ],
    "clustering": [
        {"key": "algorithm", "question": "Which clustering algorithm (k-means, HDBSCAN, agglomerative, ...)?"},
        {"key": "metric", "question": "Which distance metric?"},
        {"key": "n_clusters", "question": "How is the number of clusters chosen (fixed k, sweep + criterion, density-based)?"},
        {"key": "reduction", "question": "Dimensionality reduction before clustering (none, PCA-n, UMAP + params)?"},
        {"key": "noise", "question": "How are noise / outlier points handled?"},
        {"key": "seed", "question": "Random seed(s)? Single run or averaged over several seeds?"},
        {"key": "evaluation", "question": "Which metric decides clustering quality (silhouette, DBCV, labels, ...)?"},
    ],
    "custom": [],
}
