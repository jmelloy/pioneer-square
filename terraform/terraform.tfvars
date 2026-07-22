domain_name  = "pioneer-square.melloy.life"
frontend_url = "https://pioneer-square.melloy.life"

guild_id              = "dnsid"
foreman_provider      = "bedrock"
foreman_bedrock_model = "arn:aws:bedrock:us-east-1:446872464738:inference-profile/us.anthropic.claude-sonnet-4-6"

# Discord (bot token lives in SSM). channel_id / allowed_role_ids are not in
# .env — defaults ("") leave those features off.
discord_application_id      = "1522950582661283850"
discord_public_key          = "0f580ff639cdcd2cf79d0d7f10935c034c03d24ea89ebc506416ee2c8cd4f034"
discord_pioneer_guild_slug  = "abc123"
discord_stream_tasks        = "true"
discord_gateway_enabled     = "true"
discord_dev_guild_id        = "1522752579157622876"
discord_pr_debounce_seconds = "150"
