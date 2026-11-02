#pragma once
// LiteNativeSpeed/v1: pinned GGUF estimator-derived speed, not WAV resampling.
// This header is compiled into the isolated owner-native build, NOT used for GPU attestation.
#include <cerrno>
#include <climits>
#include <cmath>
#include <cstdlib>
#include <cstring>
inline bool lite_speed_valid(float speed) {
    return std::isfinite(speed) && speed >= 0.75f && speed <= 1.25f;
}
inline bool lite_speed_parse(const char * raw, float * result) {
    if (!raw || !*raw || !result) return false;
    // strtof accepts leading whitespace; the public CLI only accepts a canonical number.
    if (*raw==' ' || *raw=='\t' || *raw=='\n') return false;
    errno=0;
    char * end=nullptr;
    float value=std::strtof(raw,&end);
    if (end==raw || !end || *end!='\0' || errno==ERANGE || !lite_speed_valid(value)) return false;
    *result=value;
    return true;
}
inline int lite_speed_budget(float estimate,float speed) {
    // Input is the estimator's floating value BEFORE int truncation.
    if (!std::isfinite(estimate) || estimate<=0) return 1;
    double adjusted=(double)estimate/(double)speed;
    if (!std::isfinite(adjusted) || adjusted>=(double)INT_MAX) return INT_MAX;
    int budget=(int)adjusted;
    return budget<1?1:budget;
}
