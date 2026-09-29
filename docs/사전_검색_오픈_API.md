- 오픈 API 요청 URL

https://stdict.korean.go.kr/api/search.do

- 검색 요청 변수(Request Parameters)

| 요청 변수 | 타입 | 허용값 | 필수/선택 | 설명 |
| --- | --- | --- | --- | --- |
| key | string | 16진수 32자리 | 필수 | 인증 키 |
| q | string | - | 필수 | 검색어(UTF-8 인코딩) |
| req_type | string | xml  <br>json | 선택 | 요청 타입(기본값 xml) |
| start | integer | 1~1000 | 선택 | 검색의 시작 번호(기본값 1) |
| num | integer | 10~100 | 선택 | 결과 출력 건수(기본값 10) |
| advanced | string | n<br>y | 선택 | - 자세히 찾기 여부(기본값 n)<br>y: 자세히 찾기 사용<br>n: 자세히 찾기 미사용 |
| ※ 하단의 요청 변수들을 사용하시려면 자세히 찾기 여부(기본값 n)인 "advanced" 요청 변수를 'y'로 하셔야 합니다. |  |  |  |  |
| target | integer | 1 ~ 11 | 선택 | - 찾을 대상(기본값 1)<br>1: 표제어<br>2: 원어<br>3: 어원<br>4: 발음<br>5: 활용<br>6: 문형<br>7: 문법<br>8: 뜻풀이<br>9: 용례<br>10: 용례 출전<br>11: 용례 번역 |
| method | string | exact<br>include<br>start<br>end<br>wildcard | 선택 | - 검색 방식(기본값: exact)<br>exact: 일치 검색<br>include: 포함 검색<br>start: 시작<br>end: 끝<br>wildcard: 와일드카드 검색 |
| type1 | array of string | all<br>word<br>phrase<br>idiom<br>proverb | 선택 | - 구분 1(기본값 all)<br>all: 전체<br>- 아래 값을 다중 선택할 수 있도록 콤마(,)로 구분하여 나열한다.<br>word: 어휘<br>phrase: 구<br>idiom: 관용구<br>proverb: 속담 |
| type2 | array of string | all<br>native<br>chinese<br>loanword<br>hybrid | 선택 | - 구분 2(기본값 all)<br>all: 전체<br>- 아래 값을 다중 선택할 수 있도록 콤마(,)로 구분하여 나열한다.<br>native: 고유어<br>chinese: 한자어<br>loanword: 외래어<br>hybrid: 혼종어 |
| pos | array of integer | 0~15 | 선택 | - 품사(기본값 0)<br>0: 전체<br>- 아래 값을 다중 선택할 수 있도록 콤마(,)로 구분하여 나열한다.<br>1: 명사<br>2: 대명사<br>3: 수사<br>4: 조사<br>5: 동사<br>6: 형용사<br>7: 관형사<br>8: 부사<br>9: 감탄사<br>10: 접사<br>11: 의존 명사<br>12: 보조 동사<br>13: 보조 형용사<br>14: 어미<br>15: 품사 없음 |
| cat | array of integer | 0~67 | 선택 | - 전문 분야(기본값 0)<br>0: 전체<br>- 아래 값을 다중 선택할 수 있도록 콤마(,)로 구분하여 나열한다.<br>1: 언어<br>2: 문학<br>3: 역사<br>4: 철학<br>5: 교육<br>6: 민속<br>7: 인문 일반<br>8: 법률<br>9: 군사<br>10: 경영<br>11: 경제<br>12: 복지<br>13: 정치<br>14: 매체<br>15: 행정<br>16: 심리<br>17: 사회 일반<br>18: 지구<br>19: 지리<br>20: 해양<br>21: 천문<br>22: 환경<br>23: 생명<br>24: 동물<br>25: 식물<br>26: 천연자원<br>27: 수학<br>28: 물리<br>29: 화학<br>30: 자연 일반<br>31: 농업<br>32: 수산업<br>33: 임업<br>34: 광업<br>35: 공업<br>36: 서비스업<br>37: 산업 일반<br>38: 의학<br>39: 약학<br>40: 한의<br>41: 수의<br>42: 식품<br>43: 보건 일반<br>44: 건설<br>45: 교통<br>46: 기계<br>47: 전기·전자<br>48: 재료<br>49: 정보·통신<br>50: 공학 일반<br>51: 체육<br>52: 연기<br>53: 영상<br>54: 무용<br>55: 음악<br>56: 미술<br>57: 복식<br>58: 공예<br>59: 예체능 일반<br>60: 가톨릭<br>61: 기독교<br>62: 불교<br>63: 종교 일반<br>64: 인명<br>65: 지명<br>66: 책명<br>67: 고유명 일반 |
| multimedia | array of integer | 0~6 | 선택 | - 멀티미디어(기본값 0)<br>0: 전체<br>- 아래 값을 다중 선택할 수 있도록 콤마(,)로 구분하여 나열한다.<br>1: 사진<br>2: 삽화<br>3: 동영상<br>4: 애니메이션<br>5: 소리<br>6: 없음 |
| letter_s | integer | 1 ~ | 선택 | - 음절 수 시작(기본값 1) |
| letter_e | integer | 1 ~ | 선택 | - 음절 수 끝(기본값 1) |
| update_s | integer | yyyymmdd | 선택 | - 고친 날짜 시작일 |
| update_e | integer | yyyymmdd | 선택 | - 고친 날짜 종료일 |

- 검색 출력 결과 필드(Response field)

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| channel | - | 결과를 포함하는 컨테이너 |
| title | string | 표준국어대사전 오픈 API 제목 (고정값: 표준국어대사전 오픈 API - 사전 검색) |
| link | string | 표준국어대사전 URL(고정값: https://stdict.korean.go.kr) |
| description | string | 오픈 API 서비스 설명(고정값: 표준국어대사전 오픈 API – 사전 검색 결과) |
| lastBuildDate | datetime | 검색 결과를 생성한 시간 |
| total | integer | 검색된 전체 어휘 개수 |
| start | integer | 검색 결과 시작 번호 |
| num | integer | 검색 결과로 제공하는 어휘 개수 |
| num_sense | integer | 검색 결과로 제공하는 의미 개수 |
| item | - | 개별 검색 결과를 포함하는 컨테이너. ‘num’만큼 반복함 |
| word | string | 표제어 |
| sup_no | string | 어깨번호 |
| sense | - | 개별 의미를 포함하는 컨테이너 |
| sense_no | string | 의미 번호 |
| definition | string | 뜻풀이 |
| pos | string | 품사 |
| link | string | 사전 내용 보기 URL |
| type | string | 범주(일반어) |
| cat | string | 전문 분야 |
| origin | string | 원어 |
| syntacticArgument | string | 문형 |
| syntacticAnnotation | string | 문법 |

- 검색 에러 메시지(error message)

| 에러 코드 | 에러 메시지 | 설명 |
| --- | --- | --- |
| 000 | System error | 시스템 에러 |
| 020 | Unregistered key | 등록되지 않은 키 |
| 021 | Your key is temporary unavailable | 일시적으로 사용 중지된 인증 키 |
| 100 | Incorrect query request | 부적절한 쿼리 요청. query 필드 자체가 없는 경우에 발생하는 에러 메시지 |
| 101 | Invalid target value | 부적절한 검색 필드 |
| 102 | Invalid method value | 부적절한 검색 방식 |
| 103 | Invalid num value | 부적절한 검색 개수 |
| 104 | Invalid start value | 부적절한 start 값 |
| 105 | Invalid sort value | 부적절한 정렬순 |
| 106 | Invalid advanced value | 부적절한 자세히 찾기 여부 |
| 200 | Invalid type1 value | 부적절한 type1 값 |
| 201 | Invalid type2 value | 부적절한 type2 값 |
| 202~209 | - | 예약 |
| 210 | Invalid pos value | 부적절한 품사 |
| 212 | Invalid cat value | 부적절한 전문 분야 |
| 213 | Invalid multimedia value | 부적절한 멀티미디어 |
| 214 | Invalid letter_s value | 부적절한 음절 수 시작 |
| 215 | Invalid letter_e value | 부적절한 음절 수 종료 |
| 216 | Invalid update_s value | 부적절한 고친 날짜 시작일 |
| 217 | Invalid update_e value | 부적절한 고친 날짜 종료일 |

- 검색 출력 메시지 XML 예시

	<?xml version="1.0" encoding="UTF-8" ?>
	\<xml version="2.0">
		\<channel>
			<title> 표준 국어 대사전 개발 지원(Open API) - 사전 검색 </title>
			<link> https://stdict.korean.go.kr </link>
			\<description> 표준 국어 대사전 개발 지원(Open API) – 사전 검색 결과\</description>
			\<lastBuildDate> 20190115141020\</lastBuildDate>
			\<total>5\</total>
			\<start>1\</start>
			\<num>10\</num>
			\<item>
				<target_code>404765</target_code>
				\<word>나무\</word>
				<sup_no>1</sup_no>
				\<pos>명사\</pos>
				\<sense>
					\<definition> 줄기나 가지가 목질로 된 여러해살이 식물.\</definition>
					<link><![CDATA[https://stdict.korean.go.kr/search/searchView.do?word_no=404765&searchKeywordTo=3]]></link>
					\<type>일반어\</type>
				\</sense>
			\</item>
			\<item>
				<target_code>57033</target_code>
				\<word>나무\</word>
				<sup_no>2</sup_no>
				\<pos>명사\</pos>
				\<sense>
					\<definition>소 장수들의 은어로, 팔백 냥을 이르던 말.\</definition>
					<link><![CDATA[https://stdict.korean.go.kr/search/searchView.do?word_no=57033&searchKeywordTo=3]]></link>
					\<type>일반어\</type>
				\</sense>
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
	        "link": "https://stdict.korean.go.kr",
	        "description": "표준 국어 대사전 개발 지원(Open API) – 사전 검색 결과",
	        "lastbuilddate": "20190115141020",
	        "total": 5,
	        "start": 1,
	        "num": 10,
	        "item": [
	            {
	                "target_code": 404765,
	                "word": "나무",
	                "sup_no": 1,
	                "pos": "명사",
	                "sense": {
	                    "definition": "줄기나 가지가 목질로 된 여러해살이 식물.",
	                    "link": {
	                    	https://stdict.korean.go.kr/search/searchView.do?word_no=404765&searchKeywordTo=3
	                    },
	                    "type": "일반어"
	                }
	            },
	            {
	                "target_code": 57033,
	                "word": "나무",
	                "sup_no": 2,
	                "pos": "명사",
	                "sense": {
	                    "definition": "소 장수들의 은어로, 팔백 냥을 이르던 말.",
	                    "link": {
	                  	https://stdict.korean.go.kr/search/searchView.do?word_no=57033&searchKeywordTo=3
	                    },
	                    "type": "일반어"
	                }
	            }
	        ]
	    }
	}

- 검색 출력 메시지 JSON 예시

| 에러 메시지 JSON 구조                                                              | 에러 메시지 JSON 예시                                                                   |
| --------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| {<br>"error": {<br>"error_code": "에러 코드",<br>"message": "에러 메시지 "<br>}<br>} | {<br>"error": {<br>"error_code": 20,<br>"message": "Unregistered key "<br>}<br>} |