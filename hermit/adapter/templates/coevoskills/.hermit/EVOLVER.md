# Fixed actor executable adapter

Update only skills/<training-id>/evo-*/** with the original CoEvoSkills loop's
schema-validated exported packages. Keep the seed ADAPTER.md, exports, reader,
actor and judge unchanged. The fixed reader retrieves reusable Skills for public
queries. Do not include private tests, answers, histories or generator artifacts
in the learned package. Final evaluation freezes the complete adapter.
