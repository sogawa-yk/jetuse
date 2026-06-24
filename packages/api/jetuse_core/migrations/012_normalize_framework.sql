UPDATE agents SET framework = 'openai_agents' WHERE framework IN ('native', 'agents_sdk', 'hosted')
