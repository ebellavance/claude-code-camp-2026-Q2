module Boukensha
  # Static model → capability table.
  #
  # `context_window` is a known *model* fact — the physical input ceiling — not a
  # value the user sets. The agent looks it up from its configured model id; the
  # user never configures it in settings.yaml. Unknown models fall back to a
  # conservative default so an unrecognised id can't silently assume a huge window.
  module Models
    TABLE = {
      "claude-opus-4-8"   => { context_window: 200_000 },
      "claude-sonnet-4-6" => { context_window: 200_000 },
      "claude-haiku-4-5"  => { context_window: 200_000 },
    }.freeze

    DEFAULT_CONTEXT_WINDOW = 32_000

    def self.context_window(model)
      TABLE.dig(model.to_s, :context_window) || DEFAULT_CONTEXT_WINDOW
    end
  end
end
