require "json"
require "fileutils"
require "securerandom"
require "time"

module Boukensha
  class Logger
    DEFAULT_SESSION_DIR = "sessions".freeze

    attr_reader :session_id, :path

    def initialize(session_id: nil, dir: nil, log: nil, snapshot: {})
      @session_id = session_id || generate_session_id
      @path       = log || File.join(dir || default_dir, "#{@session_id}.jsonl")

      FileUtils.mkdir_p(File.dirname(@path))
      @log_io = File.open(@path, "a")
      write_log({ phase: "session_start" }.merge(snapshot))
    end

    def turn(n:)
      write_log(phase: "turn", n: n)
    end

    def iteration(n:, max:)
      write_log(phase: "iteration", n: n, max: max)
    end

    def limit_reached(kind:, n:, max:)
      write_log(phase: "limit_reached", kind: kind, n: n, max: max)
    end

    def turn_end(reason:, iterations:, tokens: nil)
      write_log(phase: "turn_end", reason: reason, iterations: iterations, tokens: tokens)
    end

    def prompt(messages:, tools:, context_window:)
      write_log(
        phase:          "prompt",
        message_count:  messages.size,
        messages:       messages.map { |m| serialize_message(m) },
        tool_count:     tools.size,
        tools:          tools.keys,
        context_window: context_window
      )
    end

    def compaction(before:, dropped:, context_window:)
      write_log(phase: "compaction", before: before, dropped: dropped, context_window: context_window)
    end

    def tool_call(name:, args:)
      write_log(phase: "tool_call", name: name, args: args)
    end

    def tool_result(name:, result:, ok: true, error: nil)
      write_log(phase: "tool_result", name: name, result: result.to_s, ok: ok, error: error)
    end

    def response(text:, usage: nil, stop_reason: nil)
      write_log(phase: "response", text: text.to_s.strip, usage: usage, stop_reason: stop_reason)
    end

    def reasoning(text:, redacted: false)
      write_log(phase: "reasoning", text: text.to_s, redacted: redacted)
    end

    def plan(text:)
      write_log(phase: "plan", text: text.to_s.strip)
    end

    def raw(data:)
      return unless Boukensha.debug?

      write_log(phase: "raw", data: data)
    end

    def subscribe(&block)
      @subscribers ||= []
      @subscribers << block
    end

    def close
      @log_io&.close
    end

    private

    def default_dir
      File.join(Boukensha.config.dir, DEFAULT_SESSION_DIR)
    end

    def write_log(event)
      @log_io.puts JSON.generate(event.merge(session_id: @session_id, at: Time.now.iso8601))
      @log_io.flush
      @subscribers&.each { |s| s.call(event) }
    end

    def generate_session_id
      "#{Time.now.utc.strftime("%Y%m%dT%H%M%SZ")}-#{SecureRandom.hex(4)}"
    end

    def serialize_message(msg)
      { role: msg.role, content: msg.content }
    end
  end
end
