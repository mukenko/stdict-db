- 오픈 API 요청 URL

https://stdict.korean.go.kr/api/view.do

- 검색 요청 변수(Request Parameters)

| 요청 변수 | 타입 | 허용값 | 필수/선택 | 설명 |
| --- | --- | --- | --- | --- |
| key | string | 16진수 32자리 | 필수 | 인증 키 |
| method | string | word_info  <br>target_code | 필수 | -검색 방식(기본값 word_info)  <br>word_info: 표제어 정보(표제어 + 어깨번호)  <br>target_code: *대상 코드(target_code) |
| req_type | string | xml  <br>json | 선택 | 요청 타입(기본값 xml) |
| q | string | - | 필수 | 검색어(UTF-8 인코딩) |

- 검색 출력 결과 필드(Response field)

| 상위 필드 | 필드 | 타입 | 필수/선택 | 설명 |
| --- | --- | --- | --- | --- |
| - | channel | - | 필수 | 결과를 포함하는 컨테이너 |
| channel | title | String | 필수 | 표준국어대사전 오픈 API 제목 (고정값: 표준국어대사전 오픈 API - 사전 검색) |
| channel | link | String | 필수 | 표준국어대사전 URL |
| channel | description | String | 필수 | 오픈 API 서비스 설명(고정값: 표준국어대사전 오픈 API – 사전 내용 검색 결과) |
| channel | lastBuildDate | datetime | 필수 | 검색 결과를 생성한 시간 |
| channel | total | integer | 필수 | 검색 건수('1' 결과 있음, '0' 결과 없음) |
| channel | item | - | 필수 | 사전 전체 항목을 포함하는 컨테이너 |
| item | target_code | String | 필수 | 표제어 |
| item | word_info | - | 필수 | 형태 항목을 포함하는 컨테이너 |
| word_info | word | String | 필수 | 표제어 |
| word_info | word_unit | String | 필수 | 구성 단위 |
| word_info | word_type | String | 필수 | 고유어 여부 |
| word_info | original_language_info | - | 필수 | 원어 항목을 포함하는 컨테이너 |
| original_language_info | original_language | String | 필수 | 원어 |
| original_language_info | language_type | String | 필수 | 언어 |
| original_language_info | language_type | String | 필수 | 언어 |
| word_info | pronunciation_info | - | 필수 | 발음 항목을 포함하는 컨테이너 |
| pronunciation_info | pronunciation | String | 필수 | 발음 |
| word_info | conju_info | - | 필수 | 활용, 준말 항목을 포함하는 컨테이너 |
| conju_info | conjugation_info | - | 필수 | 활용 항목을 포함하는 컨테이너 |
| conjugation_info | conjugation | String | 필수 | 활용 |
| conjugation_info | pronunciation_info | - | 필수 | 발음 항목을 포함하는 컨테이너 |
| pronunciation_info | pronunciation | String | 필수 | 발음 |
| conju_info | abbreviation_info | - | 필수 | 준말 항목을 포함하는 컨테이너 |
| abbreviation_info | abbreviation | String | 필수 | 준말 |
| abbreviation_info | pronunciation_info | - | 필수 | 발음 항목을 포함하는 컨테이너 |
| pronunciation_info | pronunciation | String | 필수 | 발음 |
| word_info | relation_info | - | 필수 | 관련 어휘 항목을 포함하는 컨테이너 |
| relation_info | word | String | 필수 | 표제어 |
| relation_info | type | String | 필수 | 유형 |
| relation_info | link_target_code | String | 필수 | 링크 대상 코드 |
| relation_info | link | String | 필수 | 링크 |
| word_info | origin | String | 필수 | 어원 |
| word_info | allomorph | String | 필수 | 이형태 |
| word_info | lexical_info | - | 필수 | 어휘 관계를 포함하는 컨테이너 |
| lexical_info | word | String | 필수 | 표제어 |
| lexical_info | unit | String | 필수 | 구분(어휘, 품사, 공통 문형, 의미) |
| lexical_info | type | String | 필수 | 유형 |
| lexical_info | link_target_code | String | 필수 | 링크 대상 코드(어휘 : target_code, 품사 : pos_code, 공통 문형 : comm_pattern_code, 의미 : sense_code) |
| lexical_info | link | String | 필수 | 링크 |
| word_info | pos_info | - | 필수 | 품사 항목을 포함하는 컨테이너 |
| pos_info | pos_code | String | 필수 | 픔사 대상 코드 |
| pos_info | pos | String | 필수 | 픔사 |
| pos_info | lexical_info | - | 필수 | 어휘 관계를 포함하는 컨테이너 |
| lexical_info | word | String | 필수 | 표제어 |
| lexical_info | unit | String | 필수 | 구분(어휘, 품사, 공통 문형, 의미) |
| lexical_info | type | String | 필수 | 유형 |
| lexical_info | link_target_code | String | 필수 | 링크 대상 코드(어휘 : target_code, 품사 : pos_code, 공통 문형 : comm_pattern_code, 의미 : sense_code) |
| lexical_info | link | String | 필수 | 링크 |
| pos_info | comm_pattern_info | - | 필수 | 공통 문형 항목을 포함하는 컨테이너 |
| comm_pattern_info | comm_pattern_code | String | 필수 | 공통 문형 대상 코드 |
| comm_pattern_info | pattern_info | - | 필수 | 문형 항목을 포함하는 컨테이너 |
| pattern_info | pattern | String | 필수 | 문형 |
| comm_pattern_info | grammar_info | - | 필수 | 문법 항목을 포함하는 컨테이너 |
| grammar_info | grammar | String | 필수 | 문법 |
| comm_pattern_info | lexical_info | - | 필수 | 어휘 관계를 포함하는 컨테이너 |
| lexical_info | word | String | 필수 | 표제어 |
| lexical_info | unit | String | 필수 | 구분(어휘, 품사, 공통 문형, 의미) |
| lexical_info | type | String | 필수 | 유형 |
| lexical_info | link_target_code | String | 필수 | 링크 대상 코드(어휘 : target_code, 품사 : pos_code, 공통 문형 : comm_pattern_code, 의미 : sense_code) |
| lexical_info | link | String | 필수 | 링크 |
| comm_pattern_info | sense_info | - | 필수 | 의미 항목을 포함하는 컨테이너 |
| sense_info | sense_code | String | 필수 | 의미 대상 코드 |
| sense_info | type | String | 필수 | 범주 |
| sense_info | definition | String | 필수 | 뜻풀이 |
| sense_info | definition_original | String | 필수 | 뜻풀이(어휘 링크 포함) |
| sense_info | scientific_name | String | 필수 | 학명 |
| sense_info | sense_pattern_info | - | 필수 | 문형 항목을 포함하는 컨테이너 |
| sense_pattern_info | pattern | String | 필수 | 문형 |
| sense_info | sense_grammar_info | - | 필수 | 문법 항목을 포함하는 컨테이너 |
| sense_grammar_info | grammar | String | 필수 | 문법 |
| sense_info | cat_info | - | 필수 | 전문 분야 항목을 포함하는 컨테이너 |
| cat_info | cat | String | 필수 | 전문 분야 |
| sense_info | example_info | - | 필수 | 용례 항목을 포함하는 컨테이너 |
| example_info | example | String | 필수 | 용례 |
| example_info | source | String | 필수 | 출전 |
| example_info | origin | String | 필수 | 원문 |
| example_info | translation | String | 필수 | 번역 |
| sense_info | translation_info | - | 필수 | 대역어 항목을 포함하는 컨테이너 |
| translation_info | translation | String | 필수 | 대역 |
| translation_info | language_type | String | 필수 | 언어 |
| sense_info | multimedia_info | - | 필수 | 멀티미디어 항목을 포함하는 컨테이너 |
| multimedia_info | label | String | 필수 | 제목 |
| multimedia_info | type | String | 필수 | 유형 |
| multimedia_info | link | String | 필수 | 링크 |
| sense_info | lexical_info | - | 필수 | 어휘 관계를 포함하는 컨테이너 |
| lexical_info | word | String | 필수 | 표제어 |
| lexical_info | unit | String | 필수 | 구분(어휘, 품사, 공통 문형, 의미) |
| lexical_info | type | String | 필수 | 유형 |
| lexical_info | link_target_code | String | 필수 | 링크 대상 코드(어휘 : target_code, 품사 : pos_code, 공통 문형 : comm_pattern_code, 의미 : sense_code) |
| lexical_info | link | String | 필수 | 링크 |

- 검색 에러 메시지(error message)

| 에러 코드 | 에러 메시지 | 설명 |
| --- | --- | --- |
| 000 | System error | 시스템 에러 |
| 020 | Unregistered key | 등록되지 않은 키 |
| 021 | Your key is temporary unavailable | 일시적으로 사용 중지된 인증 키 |
| 100 | Incorrect query request | 부적절한 쿼리 요청. query 필드 자체가 없는 경우에 발생하는 에러 메시지 |
| 102 | Invalid method value | 부적절한 검색 방식 |

- 검색 출력 메시지 XML 예시

	<?xml version="1.0" encoding="UTF-8" ?>
	\<xml version="2.0">
		\<channel>
			<title> 표준 국어 대사전 개발 지원(Open API) - 사전 검색 </title>
			<link> https://stdict.korean.go.kr </link>
			\<description> 표준 국어 대사전 개발 지원(Open API) – 사전 검색 결과 \</description>
			\<lastBuildDate>2\</lastBuildDate>
			\<total>1\</total>
			\<item>
				<target_code>404765</target_code>
				<word_info>
					\<word>나무\</word>
					<word_unit>단어</word_unit>
					<word_type>고유어</word_type>
				</word_info>
				<pos_info>
					\<pos>명사\</pos>
					<comm_pattern_info>
						<sense_info>
							\<type>일반어\</type>
							\<definition>줄기나 가지가 목질로 된 여러해살이 식물.\</definition>
							<example_info>
								\<example>나무가 우거진 산.\</example>
							</example_info>
							<example_info>
								\<example>나무가 울창한 숲.\</example>
							</example_info>
						</sense_info>
					</comm_pattern_info>
				</pos_info>
			\</item>
		\</channel>
	\</xml>

- 검색 출력 메시지 XML 예시

| 에러 메시지 XML 구조 | 에러 메시지 XML 예시 |
| --- | --- |
| <?xml version="1.0" encoding="UTF-8" ?><br>\<error><br><error_code>에러 코드</error_code><br>\<message>에러 메시지 \</message><br>\</error> | <?xml version="1.0" encoding="UTF-8" ?><br>\<error><br><error_code>020</error_code><br>\<message>Unregistered key \</message><br>\</error> |

- 검색 출력 메시지 JSON 예시

{
    "channel": {
        "title": "표준 국어 대사전 개발 지원(Open API) - 사전 검색 ",
        "link": "https://stdict.korean.go.kr ",
        "description": "표준 국어 대사전 개발 지원(Open API) – 사전 검색 결과 ",
        "lastbuilddate": 2,
        "total": 1,
        "item": {
            "target_code": 404765,
            "word_info": {
                "word": "나무",
                "word_unit": "단어",
                "word_type": "고유어"
            },
            "pos_info": {
                "pos": "명사",
                "comm_pattern_info": {
                    "sense_info": {
                        "type": "일반어",
                        "definition": "줄기나 가지가 목질로 된 여러해살이 식물.",
                        "example_info": [
                            {
                                "example": "나무가 우거진 산."
                            },
                            {
                                "example": "나무가 울창한 숲."
                            }
                        ]
                    }
                }
            }
        }
    }
}

- 검색 출력 메시지 JSON 예시

| 에러 메시지 JSON 구조                                                              | 에러 메시지 JSON 예시                                                                   |
| --------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| {<br>"error": {<br>"error_code": "에러 코드",<br>"message": "에러 메시지 "<br>}<br>} | {<br>"error": {<br>"error_code": 20,<br>"message": "Unregistered key "<br>}<br>} |