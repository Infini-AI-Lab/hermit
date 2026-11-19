# Sentiment classification

Classify each review as positive, negative, or neutral. The judge requires a JSON object with exactly one key,
"label", and no surrounding prose or Markdown. Incorrect training attempts receive the formatting requirement
and the reference label. Learn a general classification and output procedure; do not copy individual reviews
or their answers into the adapter.

This small, hand-written dataset is an integration smoke test. It does not estimate real-world accuracy.
The learner receives training rows and feedback; validation and held-out labels are not included in those
records. Validation tasks only score candidate adapters.
