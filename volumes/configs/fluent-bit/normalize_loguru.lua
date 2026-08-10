-- Loguru serializes metadata below `record`. Flatten the fields used for
-- querying, then discard the nested object so OpenObserve does not index
-- names such as `record_level_name` and `record_level_no`.
function normalize_loguru(tag, timestamp, record)
    if record["text"] ~= nil then
        record["message"] = record["text"]
        record["text"] = nil
    end

    local loguru_record = record["record"]

    if type(loguru_record) ~= "table" then
        return 0, timestamp, record
    end

    local level = loguru_record["level"]
    if type(level) == "table" then
        record["level"] = level["name"]
        record["level_no"] = level["no"]
    end

    record["record"] = nil
    return 2, timestamp, record
end
