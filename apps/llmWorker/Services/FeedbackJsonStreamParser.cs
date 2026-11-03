using System.Text;
using System.Text.Json;

namespace llmWorker.Services;

public sealed class FeedbackJsonStreamParser
{
    private const string FeedbackProperty = "\"feedback\"";
    private readonly StringBuilder _json = new();
    private int _readIndex;
    private bool _valueStarted;
    private bool _valueEnded;
    private bool _escaped;
    private int _unicodeDigitsRemaining;
    private int _unicodeValue;
    private char? _pendingHighSurrogate;

    public string Append(string fragment)
    {
        if (_valueEnded && fragment.Length > 0)
        {
            _json.Append(fragment);
            return string.Empty;
        }

        _json.Append(fragment);
        var output = new StringBuilder();

        if (!_valueStarted && !TryStartFeedbackValue())
        {
            return string.Empty;
        }

        while (_readIndex < _json.Length && !_valueEnded)
        {
            var value = _json[_readIndex++];
            if (_unicodeDigitsRemaining > 0)
            {
                var digit = value switch
                {
                    >= '0' and <= '9' => value - '0',
                    >= 'a' and <= 'f' => value - 'a' + 10,
                    >= 'A' and <= 'F' => value - 'A' + 10,
                    _ => throw new JsonException("The feedback field contains an invalid Unicode escape.")
                };
                _unicodeValue = (_unicodeValue << 4) | digit;
                _unicodeDigitsRemaining--;
                if (_unicodeDigitsRemaining == 0)
                {
                    AppendDecodedCharacter((char)_unicodeValue, output);
                    _unicodeValue = 0;
                }

                continue;
            }

            if (_escaped)
            {
                _escaped = false;
                switch (value)
                {
                    case '"': output.Append('"'); break;
                    case '\\': output.Append('\\'); break;
                    case '/': output.Append('/'); break;
                    case 'b': output.Append('\b'); break;
                    case 'f': output.Append('\f'); break;
                    case 'n': output.Append('\n'); break;
                    case 'r': output.Append('\r'); break;
                    case 't': output.Append('\t'); break;
                    case 'u': _unicodeDigitsRemaining = 4; break;
                    default: throw new JsonException("The feedback field contains an invalid escape.");
                }

                continue;
            }

            if (value == '\\')
            {
                _escaped = true;
            }
            else if (value == '"')
            {
                if (_pendingHighSurrogate is not null)
                {
                    throw new JsonException("The feedback field ends with an incomplete Unicode surrogate pair.");
                }

                _valueEnded = true;
            }
            else if (value < 0x20)
            {
                throw new JsonException("The feedback field contains an unescaped control character.");
            }
            else
            {
                AppendDecodedCharacter(value, output);
            }
        }

        return output.ToString();
    }

    public void Complete()
    {
        if (!_valueStarted || !_valueEnded || _escaped || _unicodeDigitsRemaining > 0 ||
            _pendingHighSurrogate is not null)
        {
            throw new JsonException("The streamed response did not contain a complete feedback string.");
        }
    }

    private bool TryStartFeedbackValue()
    {
        var json = _json.ToString();
        var propertyIndex = json.IndexOf(FeedbackProperty, StringComparison.Ordinal);
        if (propertyIndex < 0)
        {
            return false;
        }

        var index = propertyIndex + FeedbackProperty.Length;
        while (index < json.Length && char.IsWhiteSpace(json[index])) index++;
        if (index == json.Length) return false;
        if (json[index++] != ':') throw new JsonException("The response feedback property is invalid.");

        while (index < json.Length && char.IsWhiteSpace(json[index])) index++;
        if (index == json.Length) return false;
        if (json[index] != '"') throw new JsonException("The response feedback property must be a string.");

        _valueStarted = true;
        _readIndex = index + 1;
        return true;
    }

    private void AppendDecodedCharacter(char value, StringBuilder output)
    {
        if (char.IsHighSurrogate(value))
        {
            if (_pendingHighSurrogate is not null)
            {
                throw new JsonException("The feedback field contains consecutive high surrogates.");
            }

            _pendingHighSurrogate = value;
            return;
        }

        if (char.IsLowSurrogate(value))
        {
            if (_pendingHighSurrogate is not { } highSurrogate)
            {
                throw new JsonException("The feedback field contains an unmatched low surrogate.");
            }

            output.Append(highSurrogate);
            output.Append(value);
            _pendingHighSurrogate = null;
            return;
        }

        if (_pendingHighSurrogate is not null)
        {
            throw new JsonException("The feedback field contains an incomplete Unicode surrogate pair.");
        }

        output.Append(value);
    }
}
