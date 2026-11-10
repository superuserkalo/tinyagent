<?php
declare(strict_types=1);

namespace TinyAgent;

final readonly class RunResult
{
    /** @var list<Message> */
    public array $history;
    /** @var list<\stdClass> */
    private array $output;

    /** @param list<Message> $history
     *  @param list<\stdClass> $output
     */
    public function __construct(public string $text, array $history, array $output)
    {
        Json::text($text, 'Final text');
        foreach (Json::list($history, 'history') as $message) {
            if (!$message instanceof UserMessage && !$message instanceof AssistantMessage && !$message instanceof ToolResultMessage) {
                throw new \InvalidArgumentException('Unsupported history message.');
            }
        }
        $this->history = array_map(static fn(Message $message): Message => $message, $history);
        $this->output = Json::copyObjects($output);
    }

    /** @return list<\stdClass> */
    public function output(): array
    {
        return Json::copyObjects($this->output);
    }
}
